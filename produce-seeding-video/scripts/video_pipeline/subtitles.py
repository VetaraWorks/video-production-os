from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


PUNCTUATION = "，,。！？!?；;：:"
MANUAL_HIGHLIGHT_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:号色|克|毫升|ml|ML|g|G|元|折|支|个|件|盒|瓶|片|%|％)?"
)


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _strip_manual_markup(text: str) -> tuple[str, list[str]]:
    highlights = [match.group(1).strip() for match in MANUAL_HIGHLIGHT_RE.finditer(text)]
    return MANUAL_HIGHLIGHT_RE.sub(lambda match: match.group(1), text), highlights


def _clean_caption_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact.strip(PUNCTUATION + " ")


def _split_phrase(text: str, max_chars: int) -> list[str]:
    text = _clean_caption_text(text)
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    cursor = text
    while cursor:
        if len(cursor) <= max_chars:
            chunks.append(cursor)
            break
        split_at: int | None = None
        for punctuation in ("，", ",", "；", ";", "：", ":", " "):
            candidate = cursor.rfind(punctuation, 0, max_chars + 1)
            if candidate >= max(2, max_chars // 2):
                split_at = candidate + (0 if punctuation == " " else 1)
                break
        if split_at is None:
            chunk_count = math.ceil(len(cursor) / max_chars)
            split_at = math.ceil(len(cursor) / chunk_count)
        chunks.append(_clean_caption_text(cursor[:split_at]))
        cursor = cursor[split_at:].strip()
    return [chunk for chunk in chunks if chunk]


def _sentence_phrases(sentence: str, max_chars: int) -> list[dict[str, Any]]:
    plain, manual_highlights = _strip_manual_markup(sentence)
    parts = re.split(rf"(?<=[{re.escape(PUNCTUATION)}])\s*", plain)
    phrases: list[dict[str, Any]] = []
    for part in parts:
        for chunk in _split_phrase(part, max_chars):
            phrases.append(
                {
                    "text": chunk,
                    "manual_highlights": [
                        keyword for keyword in manual_highlights if keyword in chunk
                    ],
                }
            )
    return phrases


def build_cues(
    sentences: list[str],
    total_duration: float,
    max_chars_per_cue: int,
    hook_end: float = 3.0,
    cta_start: float | None = None,
) -> list[dict[str, Any]]:
    if total_duration <= 0:
        raise ValueError("Subtitle duration must be positive")

    phrases: list[dict[str, Any]] = []
    for sentence in sentences:
        if sentence.strip():
            phrases.extend(_sentence_phrases(sentence, max_chars_per_cue))
    if not phrases:
        return []

    minimum = min(0.72, total_duration / len(phrases) * 0.58)
    remaining = max(0.0, total_duration - minimum * len(phrases))
    weights = [
        max(1, len(re.sub(r"\s+", "", str(phrase["text"])))) for phrase in phrases
    ]
    weight_total = sum(weights)

    cues: list[dict[str, Any]] = []
    start = 0.0
    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        if index == len(phrases) - 1:
            end = total_duration
        else:
            duration = minimum + remaining * weight / weight_total
            end = min(total_duration, start + duration)
        role = "hook" if start < hook_end else "normal"
        if cta_start is not None and start >= cta_start:
            role = "cta"
        cues.append(
            {
                "index": index + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": phrase["text"],
                "manual_highlights": phrase["manual_highlights"],
                "role": role,
            }
        )
        start = end
    return cues


def _wrap_text(text: str, max_chars: int, newline: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines = _split_phrase(text, max_chars)
    return newline.join(lines)


def build_srt(cues: list[dict[str, Any]], max_chars_per_line: int) -> str:
    entries = []
    for cue in cues:
        entries.append(
            f"{cue['index']}\n"
            f"{_srt_timestamp(float(cue['start']))} --> "
            f"{_srt_timestamp(float(cue['end']))}\n"
            f"{_wrap_text(str(cue['text']), max_chars_per_line, chr(10))}"
        )
    return "\n\n".join(entries) + ("\n" if entries else "")


def _ass_color(value: str, fallback: str) -> str:
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(value).strip())
    rgb = match.group(1) if match else fallback.lstrip("#")
    red, green, blue = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _highlight_ranges(
    text: str,
    manual: list[str],
    config: dict[str, Any],
) -> list[tuple[int, int]]:
    candidates = [keyword for keyword in manual if keyword]
    if config.get("auto_highlight", True):
        candidates.extend(
            str(keyword)
            for keyword in config.get("highlight_keywords", [])
            if str(keyword)
        )
    candidates = sorted(set(candidates), key=lambda value: (-len(value), value))

    ranges: list[tuple[int, int]] = []
    for match in NUMBER_RE.finditer(text):
        ranges.append((match.start(), match.end()))
    for keyword in candidates:
        cursor = 0
        while True:
            index = text.find(keyword, cursor)
            if index < 0:
                break
            ranges.append((index, index + len(keyword)))
            cursor = index + len(keyword)

    ranges.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int]] = []
    maximum = max(0, int(config.get("max_highlights_per_cue", 2)))
    for start, end in ranges:
        if any(start < other_end and end > other_start for other_start, other_end in selected):
            continue
        selected.append((start, end))
        if maximum and len(selected) >= maximum:
            break
    return sorted(selected)


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_inline_text(
    text: str,
    manual_highlights: list[str],
    config: dict[str, Any],
) -> str:
    ranges = _highlight_ranges(
        text,
        manual_highlights,
        config,
    )
    highlight = _ass_color(config.get("highlight_color", "#FFE66D"), "#FFE66D")
    primary = _ass_color(config.get("primary_color", "#FFFFFF"), "#FFFFFF")
    scale = max(100, int(config.get("highlight_scale_percent", 108)))

    pieces: list[str] = []
    cursor = 0
    for start, end in ranges:
        pieces.append(_ass_escape(text[cursor:start]))
        pieces.append(
            rf"{{\c{highlight}\fscx{scale}\fscy{scale}}}"
            + _ass_escape(text[start:end])
            + rf"{{\c{primary}\fscx100\fscy100}}"
        )
        cursor = end
    pieces.append(_ass_escape(text[cursor:]))
    return "".join(pieces)


def _ass_caption_text(cue: dict[str, Any], config: dict[str, Any]) -> str:
    text = str(cue["text"])
    manual_highlights = list(cue.get("manual_highlights", []))
    lines = _split_phrase(
        text,
        int(config.get("max_chars_per_line", 12)),
    )
    return r"\N".join(
        _ass_inline_text(line, manual_highlights, config) for line in lines
    )


def build_ass(
    cues: list[dict[str, Any]],
    canvas: dict[str, Any],
    config: dict[str, Any],
) -> str:
    width = int(canvas["width"])
    height = int(canvas["height"])
    font = str(config.get("font", "Microsoft YaHei")).replace(",", " ")
    font_size = int(config.get("font_size", 64))
    hook_font_size = int(config.get("hook_font_size", font_size))
    margin_v = int(config.get("margin_v", round(height * 0.25)))
    outline = int(config.get("outline", 5))
    shadow = int(config.get("shadow", 1))
    bold = -1 if config.get("bold", True) else 0
    primary = _ass_color(config.get("primary_color", "#FFFFFF"), "#FFFFFF")
    outline_color = _ass_color(config.get("outline_color", "#151515"), "#151515")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Normal,{font},{font_size},{primary},{primary},{outline_color},&H60000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,40,40,{margin_v},1
Style: Hook,{font},{hook_font_size},{primary},{primary},{outline_color},&H60000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,40,40,{margin_v},1
Style: CTA,{font},{font_size},{primary},{primary},{outline_color},&H60000000,{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for cue in cues:
        style = {"hook": "Hook", "cta": "CTA"}.get(str(cue.get("role")), "Normal")
        animation = ""
        if config.get("pop_animation", True):
            animation = r"{\fad(45,70)\fscx108\fscy108\t(0,120,\fscx100\fscy100)}"
        events.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(float(cue['start']))},"
            f"{_ass_timestamp(float(cue['end']))},"
            f"{style},,0,0,0,,{animation}{_ass_caption_text(cue, config)}"
        )
    return header + "\n".join(events) + ("\n" if events else "")


def write_subtitles(
    output_dir: Path,
    sentences: list[str],
    total_duration: float,
    canvas: dict[str, Any],
    config: dict[str, Any],
    hook_end: float = 3.0,
    cta_start: float | None = None,
    timed_cues: list[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    cues = list(timed_cues or [])
    if not cues:
        cues = build_cues(
            sentences,
            total_duration,
            int(config.get("max_chars_per_cue", 10)),
            hook_end=hook_end,
            cta_start=cta_start,
        )
    for index, cue in enumerate(cues, start=1):
        cue.setdefault("index", index)
        cue.setdefault("manual_highlights", [])
        cue.setdefault("role", "normal")
    srt_path = output_dir / str(config.get("srt_filename", "subtitles.srt"))
    srt_path.write_text(
        build_srt(cues, int(config.get("max_chars_per_line", 12))),
        encoding="utf-8",
    )

    paths = {"srt": srt_path}
    if str(config.get("format", "ass")).lower() == "ass":
        ass_path = output_dir / str(config.get("filename", "subtitles.ass"))
        ass_path.write_text(build_ass(cues, canvas, config), encoding="utf-8-sig")
        paths["primary"] = ass_path
        paths["ass"] = ass_path
    else:
        paths["primary"] = srt_path
    return paths
