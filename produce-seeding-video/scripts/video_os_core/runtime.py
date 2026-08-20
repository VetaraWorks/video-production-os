"""Portable, side-effect-free discovery for Video OS runtime dependencies.

Discovery never installs software and never mutates project state. Explicit
configuration is authoritative: an invalid explicit path is reported as
unavailable instead of being silently replaced by another executable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


Which = Callable[[str], str | None]


def _ready(path: Path, source: str, *, version: str | None = None, kind: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ready",
        "path": str(path.resolve()),
        "source": source,
        "version": version,
        "error": None,
    }
    if kind is not None:
        result["kind"] = kind
    return result


def _unavailable(component: str, message: str, *, source: str = "none") -> dict[str, Any]:
    return {
        "status": "unavailable",
        "path": None,
        "source": source,
        "version": None,
        "error": {
            "code": f"runtime.{component}.unavailable",
            "message": message,
        },
    }


def _path_from_value(value: str, *, which: Which) -> Path | None:
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    if not expanded:
        return None
    candidate = Path(expanded)
    if candidate.is_file():
        return candidate.resolve()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return None
    located = which(expanded)
    if located and Path(located).is_file():
        return Path(located).resolve()
    return None


def _version(path: Path, arguments: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            [str(path), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0].strip() if output else None


def discover_executable(
    component: str,
    *,
    explicit: str | None = None,
    environment_names: Sequence[str] = (),
    command_names: Sequence[str] = (),
    common_paths: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    version_arguments: Sequence[str] = (),
    probe_version: bool = True,
) -> dict[str, Any]:
    """Discover one executable using deterministic, auditable precedence."""
    environment = os.environ if environ is None else environ
    if explicit is not None and str(explicit).strip():
        path = _path_from_value(str(explicit), which=which)
        if path is None:
            return _unavailable(
                component,
                f"Configured {component} executable was not found: {explicit}",
                source="explicit",
            )
        version = _version(path, version_arguments) if probe_version and version_arguments else None
        return _ready(path, "explicit", version=version)

    for environment_name in environment_names:
        value = str(environment.get(environment_name) or "").strip()
        if not value:
            continue
        path = _path_from_value(value, which=which)
        if path is None:
            return _unavailable(
                component,
                f"{environment_name} does not identify an executable: {value}",
                source=f"environment:{environment_name}",
            )
        version = _version(path, version_arguments) if probe_version and version_arguments else None
        return _ready(path, f"environment:{environment_name}", version=version)

    for command_name in command_names:
        located = which(command_name)
        if located and Path(located).is_file():
            path = Path(located).resolve()
            version = _version(path, version_arguments) if probe_version and version_arguments else None
            return _ready(path, f"path:{command_name}", version=version)

    for candidate in common_paths:
        if candidate.is_file():
            path = candidate.resolve()
            version = _version(path, version_arguments) if probe_version and version_arguments else None
            return _ready(path, "common_path", version=version)

    return _unavailable(
        component,
        f"{component} executable was not found in explicit configuration, environment, or PATH",
    )


def discover_python(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    current_executable: str | None = None,
    probe_version: bool = True,
) -> dict[str, Any]:
    if explicit is not None and str(explicit).strip():
        return discover_executable(
            "python",
            explicit=explicit,
            environ=environ,
            which=which,
            version_arguments=("--version",),
            probe_version=probe_version,
        )
    environment = os.environ if environ is None else environ
    configured = str(environment.get("VIDEO_OS_PYTHON") or "").strip()
    if configured:
        return discover_executable(
            "python",
            explicit=configured,
            environ=environment,
            which=which,
            version_arguments=("--version",),
            probe_version=probe_version,
        ) | {"source": "environment:VIDEO_OS_PYTHON"}
    current = Path(current_executable or sys.executable)
    if current.is_file():
        version = _version(current, ("--version",)) if probe_version else None
        return _ready(current, "current_process", version=version)
    return discover_executable(
        "python",
        command_names=("python", "python3"),
        environ=environment,
        which=which,
        version_arguments=("--version",),
        probe_version=probe_version,
    )


def _windows_program_paths(environ: Mapping[str, str], suffixes: Sequence[str]) -> list[Path]:
    roots = [
        environ.get("ProgramFiles"),
        environ.get("ProgramFiles(x86)"),
        environ.get("LOCALAPPDATA"),
    ]
    return [Path(root) / suffix for root in roots if root for suffix in suffixes]


def discover_node(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    probe_version: bool = True,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    common = _windows_program_paths(
        environment,
        ("nodejs/node.exe", "Programs/nodejs/node.exe"),
    ) if os.name == "nt" else []
    return discover_executable(
        "node",
        explicit=explicit,
        environment_names=("VIDEO_OS_NODE",),
        command_names=("node",),
        common_paths=common,
        environ=environment,
        which=which,
        version_arguments=("--version",),
        probe_version=probe_version,
    )


def discover_ffmpeg(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    probe_version: bool = True,
) -> dict[str, Any]:
    return discover_executable(
        "ffmpeg",
        explicit=explicit,
        environment_names=("VIDEO_OS_FFMPEG",),
        command_names=("ffmpeg",),
        environ=environ,
        which=which,
        version_arguments=("-version",),
        probe_version=probe_version,
    )


def discover_ffprobe(
    explicit: str | None = None,
    *,
    ffmpeg_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    probe_version: bool = True,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    if explicit is not None and str(explicit).strip():
        return discover_executable(
            "ffprobe",
            explicit=explicit,
            environ=environment,
            which=which,
            version_arguments=("-version",),
            probe_version=probe_version,
        )
    configured = str(environment.get("VIDEO_OS_FFPROBE") or "").strip()
    if configured:
        result = discover_executable(
            "ffprobe",
            explicit=configured,
            environ=environment,
            which=which,
            version_arguments=("-version",),
            probe_version=probe_version,
        )
        result["source"] = "environment:VIDEO_OS_FFPROBE"
        return result
    if ffmpeg_path:
        ffmpeg = Path(ffmpeg_path)
        sibling = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
        if sibling.is_file():
            version = _version(sibling, ("-version",)) if probe_version else None
            return _ready(sibling, "ffmpeg_sibling", version=version)
    return discover_executable(
        "ffprobe",
        command_names=("ffprobe",),
        environ=environment,
        which=which,
        version_arguments=("-version",),
        probe_version=probe_version,
    )


def _browser_candidates(environ: Mapping[str, str]) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    if os.name == "nt":
        for kind, suffix in (
            ("chrome", "Google/Chrome/Application/chrome.exe"),
            ("edge", "Microsoft/Edge/Application/msedge.exe"),
        ):
            candidates.extend(
                (kind, path)
                for path in _windows_program_paths(environ, (suffix,))
            )
    return candidates


def discover_browser(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    common_candidates: Iterable[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    value = str(explicit or "").strip()
    source = "explicit"
    if not value:
        value = str(environment.get("VIDEO_OS_BROWSER") or "").strip()
        source = "environment:VIDEO_OS_BROWSER"
    if value:
        path = _path_from_value(value, which=which)
        if path is None:
            return _unavailable(
                "browser",
                f"Configured browser executable was not found: {value}",
                source=source,
            )
        kind = "edge" if path.name.lower().startswith("msedge") else "chrome"
        return _ready(path, source, kind=kind)

    for kind, command in (("chrome", "chrome"), ("edge", "msedge")):
        located = which(command)
        if located and Path(located).is_file():
            return _ready(Path(located), f"path:{command}", kind=kind)
    for kind, candidate in common_candidates or _browser_candidates(environment):
        if candidate.is_file():
            return _ready(candidate, "common_path", kind=kind)
    result = _unavailable(
        "browser",
        "Chrome or Microsoft Edge was not found in explicit configuration, environment, or installed locations",
    )
    result["kind"] = None
    return result


def discover_runtime(
    overrides: Mapping[str, str | None] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    current_executable: str | None = None,
    probe_versions: bool = True,
) -> dict[str, Any]:
    values = dict(overrides or {})
    python = discover_python(
        values.get("python"),
        environ=environ,
        which=which,
        current_executable=current_executable,
        probe_version=probe_versions,
    )
    node = discover_node(
        values.get("node"), environ=environ, which=which, probe_version=probe_versions
    )
    ffmpeg = discover_ffmpeg(
        values.get("ffmpeg"), environ=environ, which=which, probe_version=probe_versions
    )
    ffprobe = discover_ffprobe(
        values.get("ffprobe"),
        ffmpeg_path=ffmpeg.get("path"),
        environ=environ,
        which=which,
        probe_version=probe_versions,
    )
    browser = discover_browser(values.get("browser"), environ=environ, which=which)
    components = {
        "python": python,
        "node": node,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "browser": browser,
    }
    return {
        "schema_version": 1,
        "ok": all(item["status"] == "ready" for item in components.values()),
        "components": components,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover portable Video OS runtime dependencies")
    for name in ("python", "node", "ffmpeg", "ffprobe", "browser"):
        parser.add_argument(f"--{name}")
    parser.add_argument("--no-version-probe", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    overrides = {
        name: getattr(args, name)
        for name in ("python", "node", "ffmpeg", "ffprobe", "browser")
        if getattr(args, name)
    }
    print(
        json.dumps(
            discover_runtime(overrides, probe_versions=not args.no_version_probe),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
