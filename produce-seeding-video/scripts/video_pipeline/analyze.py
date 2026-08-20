from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .perception import load_project_perception, perception_index
from .probe import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, discover_files, probe_media


ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hook": ("hook", "开头", "钩子"),
    "talking": ("talking", "speak", "host", "口播", "真人", "人物"),
    "product": ("product", "item", "产品", "商品"),
    "detail": ("detail", "closeup", "macro", "细节", "特写"),
    "proof": ("proof", "result", "beforeafter", "效果", "对比", "使用"),
    "cta": ("cta", "buy", "order", "购买", "下单", "引导"),
    "bgm": ("bgm", "music", "配乐", "音乐"),
}
CTA_KEYWORDS = ("购买", "下单", "点击", "立即", "马上", "入手", "链接", "购物车")


def read_script(project_dir: Path) -> str:
    script_path = project_dir / "script" / "script.txt"
    if not script_path.is_file():
        raise FileNotFoundError(f"Required script not found: {script_path}")
    try:
        text = script_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = script_path.read_text(encoding="gb18030")
    text = text.strip()
    if not text:
        raise ValueError(f"Script is empty: {script_path}")
    return text


def split_script(text: str) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", text)
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", normalized)
    return [part.strip() for part in parts if part.strip()]


def classify_name(path: Path, group: str) -> list[str]:
    folded = path.stem.casefold().replace("-", "_")
    tags = [
        role
        for role, keywords in ROLE_KEYWORDS.items()
        if any(keyword.casefold() in folded for keyword in keywords)
    ]
    if path.suffix.lower() in AUDIO_EXTENSIONS and "bgm" not in tags:
        tags.append("audio")
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        if group == "raw_video" and "talking" not in tags:
            tags.append("talking")
        if group == "material" and not set(tags).intersection(
            {"hook", "product", "detail", "proof", "cta"}
        ):
            tags.append("product")
    return sorted(set(tags))


def _media_record(path: Path, project_dir: Path, group: str, ffprobe: str) -> dict[str, Any]:
    metadata = probe_media(path, ffprobe)
    return {
        "path": path.relative_to(project_dir).as_posix(),
        "group": group,
        "tags": classify_name(path, group),
        **metadata,
    }


def attach_project_perception(
    analysis: dict[str, Any],
    project_dir: Path,
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Load current Perception after ANALYZE and attach it for PLAN only."""

    media = list(analysis.get("media", []))
    references = list(analysis.get("references", []))
    all_media = media + references
    for item in all_media:
        item.pop("perception", None)
    perception, perception_warnings = load_project_perception(
        project_dir,
        all_media,
        config,
    )
    analysis.setdefault("warnings", []).extend(perception_warnings)
    indexed_perception = perception_index(perception)
    covered_video_count = 0
    perception_segment_count = 0
    for item in all_media:
        source_perception = indexed_perception.get(str(item["path"]))
        if source_perception:
            item["perception"] = source_perception
            covered_video_count += 1
            perception_segment_count += len(source_perception.get("segments", []))
    videos = [
        item
        for item in media
        if item.get("has_video") and float(item.get("duration", 0)) > 0
    ]
    analysis["perception"] = {
        "available": bool(perception),
        "provider": perception.get("provider") if perception else None,
        "input_signature": perception.get("input_signature") if perception else None,
        "covered_video_count": covered_video_count,
        "segment_count": perception_segment_count,
        "uncovered_videos": [
            item["path"]
            for item in videos
            if item["path"] not in indexed_perception
        ],
    }
    return analysis, perception


def build_analysis(
    project_dir: Path,
    config: dict[str, Any],
    ffprobe: str,
    *,
    include_perception: bool = True,
) -> dict[str, Any]:
    script = read_script(project_dir)
    sentences = split_script(script)
    if not sentences:
        sentences = [script]

    media: list[dict[str, Any]] = []
    warnings: list[str] = []
    for group in ("raw_video", "material"):
        folder = project_dir / group
        files = discover_files(folder, VIDEO_EXTENSIONS | AUDIO_EXTENSIONS)
        for path in files:
            try:
                media.append(_media_record(path, project_dir, group, ffprobe))
            except RuntimeError as exc:
                warnings.append(str(exc))

    reference_files = discover_files(project_dir / "reference", VIDEO_EXTENSIONS)
    references: list[dict[str, Any]] = []
    for path in reference_files:
        try:
            references.append(_media_record(path, project_dir, "reference", ffprobe))
        except RuntimeError as exc:
            warnings.append(str(exc))

    videos = [item for item in media if item["has_video"] and item["duration"] > 0]
    if not videos:
        raise ValueError(
            "No usable video found under raw_video/ or material/. "
            "Reference videos are not output footage in V1."
        )

    cta_sentence = next(
        (
            sentence
            for sentence in reversed(sentences)
            if any(keyword in sentence for keyword in CTA_KEYWORDS)
        ),
        sentences[-1],
    )
    selling_points = [
        sentence
        for sentence in sentences[1:-1]
        if sentence != cta_sentence
    ][:5]

    if len(videos) < len(config["template_segments"]):
        warnings.append(
            "Unique video count is lower than template segment count; clip reuse is expected."
        )
    if not references:
        warnings.append("No reference video supplied; V1 will use the fixed template only.")

    analysis = {
        "schema_version": 1,
        "template": config["template"],
        "script": {
            "path": "script/script.txt",
            "text": script,
            "character_count": len(re.sub(r"\s+", "", script)),
            "sentence_count": len(sentences),
            "sentences": sentences,
            "hook": sentences[0],
            "selling_points": selling_points,
            "cta": cta_sentence,
        },
        "media": media,
        "references": references,
        "perception": {
            "available": False,
            "provider": None,
            "input_signature": None,
            "covered_video_count": 0,
            "segment_count": 0,
            "uncovered_videos": [item["path"] for item in videos],
        },
        "summary": {
            "video_count": len(videos),
            "audio_asset_count": len(
                [item for item in media if item["has_audio"] and not item["has_video"]]
            ),
            "total_video_duration": round(
                sum(float(item["duration"]) for item in videos), 3
            ),
        },
        "warnings": warnings,
    }
    if include_perception:
        analysis, _perception = attach_project_perception(
            analysis,
            project_dir,
            config,
        )
    return analysis
