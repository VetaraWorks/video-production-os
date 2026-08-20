#!/usr/bin/env python3
"""Compare a source Skill tree with an installed release copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


EXCLUDED_TOP_LEVEL = {"tests", ".tmp"}
EXCLUDED_DIRECTORIES = {"__pycache__", ".pytest_cache", "node_modules"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
REQUIRED_RELEASE_FILES = {
    Path("SKILL.md"),
    Path("package.json"),
    Path("package-lock.json"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("requirements-optional.txt"),
    Path("assets/default-config.json"),
    Path("scripts/video_os.py"),
    Path("scripts/knowledge_tools.py"),
    Path("scripts/run_pipeline.py"),
    Path("scripts/video_os_core/knowledge_root.py"),
    Path("scripts/video_os_core/project_manager.py"),
    Path("scripts/video_os_core/providers.py"),
    Path("scripts/video_os_core/report_manager.py"),
    Path("scripts/video_os_core/perception_providers/base.py"),
    Path("scripts/video_os_core/perception_providers/gemini_worker.py"),
    Path("scripts/video_os_core/perception_providers/qwen_api.py"),
    Path("scripts/video_os_core/perception_manager.py"),
    Path("scripts/video_os_core/review_manager.py"),
    Path("scripts/video_os_core/runtime.py"),
    Path("scripts/video_os_core/system_manager.py"),
    Path("scripts/video_os_core/worker_manager.py"),
    Path("scripts/video_os_core/state_machine.py"),
    Path("references/knowledge-root.md"),
    Path("references/provider-contract.md"),
    Path("references/qwen-api.md"),
    Path("references/setup-doctor.md"),
}


def _release_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return False
    if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    return path.suffix.lower() not in EXCLUDED_SUFFIXES


def release_files(root: Path) -> list[Path]:
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and _release_file(path, root)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_consistency(source_root: Path, installed_root: Path) -> dict[str, Any]:
    source_root = Path(source_root).expanduser().resolve()
    installed_root = Path(installed_root).expanduser().resolve()
    errors: list[str] = []
    missing: list[str] = []
    different: list[str] = []

    if not source_root.is_dir():
        errors.append(f"source Skill directory does not exist: {source_root}")
    if not installed_root.is_dir():
        errors.append(f"installed Skill directory does not exist: {installed_root}")

    files = release_files(source_root)
    source_set = set(files)
    for required in sorted(REQUIRED_RELEASE_FILES):
        if required not in source_set:
            errors.append(f"required release file is absent from source: {required.as_posix()}")

    if not errors:
        for relative in files:
            source = source_root / relative
            installed = installed_root / relative
            display = relative.as_posix()
            if not installed.is_file():
                missing.append(display)
            elif _sha256(source) != _sha256(installed):
                different.append(display)

        skill_path = installed_root / "SKILL.md"
        if skill_path.is_file():
            skill = skill_path.read_text(encoding="utf-8-sig")
            for command in (
                "python scripts/video_os.py run <project-dir> --to PLAN",
                "python scripts/video_os.py run <project-dir> --to FINAL",
            ):
                if command not in skill:
                    errors.append(f"installed SKILL.md lacks default Director command: {command}")
            if re.search(r"(?m)^\s*python\s+scripts/run_pipeline\.py(?:\s|$)", skill):
                errors.append("installed SKILL.md exposes run_pipeline.py as a direct command")

        for relative in (Path("scripts/video_os.py"), Path("scripts/knowledge_tools.py")):
            cli_path = installed_root / relative
            if not cli_path.is_file():
                continue
            cli_text = cli_path.read_text(encoding="utf-8-sig")
            if "DEFAULT_KNOWLEDGE_ROOT" in cli_text:
                errors.append(
                    f"installed {relative.as_posix()} retains a script-relative Knowledge Root"
                )
            if re.search(r"parents\[[0-9]+\]\s*/\s*[\"']knowledge[\"']", cli_text):
                errors.append(
                    f"installed {relative.as_posix()} guesses Knowledge Root from script location"
                )

        root_contract = installed_root / "scripts" / "video_os_core" / "knowledge_root.py"
        if root_contract.is_file():
            contract_text = root_contract.read_text(encoding="utf-8-sig")
            for marker in (
                "VIDEO_OS_KNOWLEDGE_ROOT",
                "unconfigured",
                "path_missing",
                "initialized_empty",
                "ready",
            ):
                if marker not in contract_text:
                    errors.append(
                        f"installed Knowledge Root contract lacks state marker: {marker}"
                    )

    return {
        "ok": not errors and not missing and not different,
        "source_root": str(source_root),
        "installed_root": str(installed_root),
        "checked_files": len(files),
        "missing": missing,
        "different": different,
        "errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("installed_root", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_consistency(args.source_root, args.installed_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
