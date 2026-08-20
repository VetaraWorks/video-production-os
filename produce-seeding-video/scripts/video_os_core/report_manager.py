"""Strictly allowlisted and redacted Public Beta diagnostic reports."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from video_os_core.runtime import discover_runtime
from video_os_core import system_manager, worker_manager, providers


REPORT_SCHEMA_VERSION = 1
SENSITIVE_KEYS = ("api_key", "apikey", "authorization", "token", "cookie", "session", "password", "secret")
PROMPT_SAFE_KEYS = {"prompt_version", "prompt_hash", "prompt_sha256"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wav", ".mp3", ".m4a", ".png", ".jpg", ".jpeg"}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _secret_values(environ: Mapping[str, str]) -> list[str]:
    values = []
    for key, value in environ.items():
        normalized = key.casefold()
        if any(term in normalized for term in SENSITIVE_KEYS) and len(str(value)) >= 6:
            values.append(str(value))
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: Any, *, environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    result = str(text)
    home = str(Path.home())
    if home:
        result = re.sub(re.escape(home), "<USER_HOME>", result, flags=re.IGNORECASE)
    for secret in _secret_values(environment):
        result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", result)
    result = re.sub(
        r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        result,
    )
    return result


def redact(value: Any, *, key: str = "", environ: Mapping[str, str] | None = None) -> Any:
    normalized = key.casefold().replace("-", "_")
    if any(term in normalized for term in SENSITIVE_KEYS):
        return "[REDACTED]"
    if "prompt" in normalized and normalized not in PROMPT_SAFE_KEYS:
        return "[OMITTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, key=str(item_key), environ=environ) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key=key, environ=environ) for item in value]
    if isinstance(value, tuple):
        return [redact(item, key=key, environ=environ) for item in value]
    if isinstance(value, str):
        return redact_text(value, environ=environ)
    return value


def _artifact(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    return payload if payload is not None else {"available": False, "path": path.as_posix()}


def _repair_summary(project_dir: Path) -> dict[str, Any]:
    plan = _read_json(project_dir / "repair" / "repair_plan.json")
    diff = _read_json(project_dir / "repair" / "repair_diff.json")
    return {
        "available": plan is not None or diff is not None,
        "plan": plan,
        "diff": diff,
    }


def _errors(state: Mapping[str, Any]) -> str:
    lines: list[str] = []
    blocked = state.get("blocked")
    if isinstance(blocked, Mapping) and blocked.get("error"):
        lines.append(f"blocked/{blocked.get('stage')}: {blocked.get('error')}")
    stages = state.get("stages")
    if isinstance(stages, Mapping):
        for stage, record in stages.items():
            if isinstance(record, Mapping) and record.get("last_error"):
                lines.append(f"{stage}: {record.get('last_error')}")
    return "\n".join(dict.fromkeys(lines)) + ("\n" if lines else "")


def _provider_snapshot(config: Mapping[str, Any] | None, data_root: Path | None,
                       environ: Mapping[str, str]) -> dict[str, Any]:
    provider_config = config.get("provider") if isinstance(config, Mapping) and isinstance(config.get("provider"), Mapping) else {}
    provider_type = str(provider_config.get("type") or "unconfigured")
    result: dict[str, Any] = {"type": provider_type}
    if provider_type == "qwen-api":
        try:
            provider = providers.get_provider(
                provider_type, script_dir=Path(__file__).resolve().parents[1],
                skill_root=Path(__file__).resolve().parents[2], options=provider_config,
                environ=environ,
            )
            result["health"] = provider.healthcheck(live=False)
        except providers.ProviderError as exc:
            result["health"] = {"ok": False, "code": exc.code, "error": str(exc)}
    elif provider_type == "gemini-worker":
        result["worker"] = worker_manager.worker_status(data_root)
    return result


def create_report(
    project_dir: Path,
    *,
    output: Path | None = None,
    data_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    project_dir = Path(project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"Project directory not found: {project_dir}")
    environment = os.environ if environ is None else environ
    generated_at = datetime.now(timezone.utc)
    destination = (
        Path(output).expanduser().resolve()
        if output is not None
        else project_dir / "reports" / f"video-os-report-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    if destination.suffix.lower() != ".zip":
        raise ValueError("report output must use .zip")
    for input_name in ("raw_video", "material", "reference"):
        try:
            destination.relative_to(project_dir / input_name)
        except ValueError:
            continue
        raise ValueError("report output cannot be written into a media input directory")

    state = _artifact(project_dir / "project_state.json")
    blocked = state.get("blocked") if isinstance(state.get("blocked"), Mapping) else {}
    system_config = system_manager.load_system_config(data_root=data_root, environ=environment)
    runtime = discover_runtime(environ=environment)
    entries: dict[str, Any] = {
        "summary.json": {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "project": project_dir.name,
            "stage": state.get("stage"),
            "status": state.get("status") or blocked.get("kind"),
            "media_included": False,
            "prompt_content_included": False,
        },
        "system.json": {
            "platform": platform.platform(),
            "os": os.name,
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
        },
        "runtime.json": runtime,
        "provider.json": _provider_snapshot(system_config, data_root, environment),
        "project_state.json": state,
        "qa_report.json": _artifact(project_dir / "output" / "qa_report.json"),
        "review.json": _artifact(project_dir / "review" / "review.json"),
        "repair_summary.json": _repair_summary(project_dir),
        "memory_application.json": _artifact(project_dir / "output" / "memory_application.json"),
    }
    errors = redact_text(_errors(state), environ=environment)
    sanitized = {name: redact(payload, environ=environment) for name, payload in entries.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sanitized.items():
            if Path(name).suffix.lower() in MEDIA_SUFFIXES:
                raise RuntimeError(f"media cannot enter report: {name}")
            archive.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        archive.writestr("errors.log", errors)
    return {
        "ok": True,
        "report": str(destination),
        "entries": sorted([*sanitized, "errors.log"]),
        "media_included": False,
        "redacted": True,
    }
