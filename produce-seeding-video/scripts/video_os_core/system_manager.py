"""User configuration and read-only diagnostics for Video OS Public Beta."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from video_os_core import knowledge
from video_os_core.knowledge_root import inspect_knowledge_root
from video_os_core.runtime import discover_runtime
from video_os_core import worker_manager
from video_os_core import providers
from video_os_core.perception_providers.qwen_api import DEFAULT_MODEL as DEFAULT_QWEN_MODEL


CONFIG_SCHEMA_VERSION = 1
CONFIG_ENV = "VIDEO_OS_CONFIG"
API_KEY_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CORE_DIR = Path(__file__).resolve().parent


class SystemConfigError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def system_paths(data_root: str | Path | None = None) -> dict[str, Path]:
    root = worker_manager.default_data_root(data_root)
    return {
        "data_root": root,
        "config_dir": root / "config",
        "config": root / "config" / "video-os.json",
        "projects": root / "projects",
        "knowledge": root / "knowledge",
        "worker": root / "worker",
        "cache": root / "cache",
        "logs": root / "logs",
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def configured_path(
    *,
    config_path: str | Path | None = None,
    data_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    explicit = str(config_path or "").strip()
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser().resolve()
    from_env = str(environment.get(CONFIG_ENV) or "").strip()
    if from_env:
        return Path(os.path.expandvars(from_env)).expanduser().resolve()
    return system_paths(data_root)["config"]


def load_system_config(
    *,
    config_path: str | Path | None = None,
    data_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    path = configured_path(config_path=config_path, data_root=data_root, environ=environ)
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        schema = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        return None
    if schema != CONFIG_SCHEMA_VERSION:
        return None
    return {**payload, "_config_path": str(path)}


def apply_system_config(
    *,
    config_path: str | Path | None = None,
    data_root: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    environment = os.environ if environ is None else environ
    config = load_system_config(
        config_path=config_path, data_root=data_root, environ=environment
    )
    if config is None:
        return None
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    data_root_value = str(config.get("data_root") or "").strip()
    knowledge_root = str(paths.get("knowledge_root") or "").strip()
    worker_config = str(config.get("worker_config") or "").strip()
    provider = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    if data_root_value:
        environment.setdefault("VIDEO_OS_DATA_ROOT", data_root_value)
    if knowledge_root:
        environment.setdefault("VIDEO_OS_KNOWLEDGE_ROOT", knowledge_root)
    if worker_config:
        environment.setdefault("VIDEO_OS_WORKER_CONFIG", worker_config)
    if provider.get("type"):
        environment.setdefault("VIDEO_OS_PERCEPTION_PROVIDER", str(provider["type"]))
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    for name, env_name in (
        ("python", "VIDEO_OS_PYTHON"),
        ("node", "VIDEO_OS_NODE"),
        ("ffmpeg", "VIDEO_OS_FFMPEG"),
        ("ffprobe", "VIDEO_OS_FFPROBE"),
        ("browser", "VIDEO_OS_BROWSER"),
    ):
        item = runtime.get(name)
        value = str(item.get("path") or "").strip() if isinstance(item, Mapping) else ""
        if value:
            environment.setdefault(env_name, value)
    return config


def configured_projects_root(config: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(config, Mapping):
        return None
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        return None
    value = str(paths.get("projects_root") or "").strip()
    return Path(value).expanduser().resolve() if value else None


def _runtime_paths(runtime: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, item in runtime.get("components", {}).items():
        result[name] = {
            "path": item.get("path"),
            "source": item.get("source"),
            "version": item.get("version"),
        }
    return result


def setup_video_os(
    data_root: str | Path | None = None,
    *,
    provider: str = "none",
    api_key_env: str = "QWEN_API_KEY",
    model: str | None = None,
    runtime_overrides: Mapping[str, str | None] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    provider = {"qwen_api": "qwen-api", "gemini_worker": "gemini-worker"}.get(
        provider, provider
    )
    if provider not in {"none", "gemini-worker", "qwen-api"}:
        raise SystemConfigError("SETUP_PROVIDER_INVALID", f"Unsupported Provider: {provider}")
    if not API_KEY_ENV_PATTERN.fullmatch(api_key_env):
        raise SystemConfigError(
            "SETUP_API_KEY_ENV_INVALID",
            "API key environment variable name is invalid",
        )
    paths = system_paths(data_root)
    existing = load_system_config(data_root=paths["data_root"])
    if existing is not None and not force:
        return {
            "ok": True,
            "status": "configured",
            "created": False,
            "preserved": True,
            "config_path": existing["_config_path"],
            "config": {key: value for key, value in existing.items() if key != "_config_path"},
            "message": "Existing Video OS user configuration was preserved. Use --force to replace configuration only.",
        }

    runtime = discover_runtime(runtime_overrides)
    for name in ("data_root", "config_dir", "projects", "knowledge", "worker", "cache", "logs"):
        paths[name].mkdir(parents=True, exist_ok=True)
    knowledge_result = knowledge.init_knowledge(paths["knowledge"], force=False)

    worker_config: str | None = None
    worker_result: dict[str, Any] | None = None
    if provider == "gemini-worker":
        try:
            worker_result = worker_manager.initialize_worker(
                paths["data_root"], overrides=runtime_overrides
            )
            worker_config = str(worker_result["config_path"])
        except worker_manager.WorkerError as exc:
            raise SystemConfigError(
                "SETUP_WORKER_RUNTIME_INCOMPLETE",
                str(exc),
                details={"code": exc.code, "details": exc.details},
            ) from exc

    provider_config: dict[str, Any] = {"type": provider}
    if provider == "qwen-api":
        provider_config.update(
            {"api_key_env": api_key_env, "model": model or DEFAULT_QWEN_MODEL}
        )
    config = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "configured_at": datetime.now(timezone.utc).isoformat(),
        "data_root": str(paths["data_root"]),
        "paths": {
            "projects_root": str(paths["projects"]),
            "knowledge_root": str(paths["knowledge"]),
            "worker_root": str(paths["worker"]),
            "cache_root": str(paths["cache"]),
            "logs_root": str(paths["logs"]),
        },
        "runtime": _runtime_paths(runtime),
        "provider": provider_config,
        "worker_config": worker_config,
    }
    _atomic_write_json(paths["config"], config)
    suggested = str(Path("D:/VideoOS")) if os.name == "nt" and Path("D:/").exists() else None
    return {
        "ok": True,
        "status": "configured",
        "created": existing is None,
        "preserved": True,
        "config_path": str(paths["config"]),
        "config": config,
        "runtime_ready": runtime["ok"],
        "worker": worker_result,
        "knowledge": knowledge_result,
        "alternative_data_root": suggested,
        "message": (
            "Setup completed. Run 'video_os.py worker login' to authenticate Gemini."
            if provider == "gemini-worker"
            else "Setup completed. Run 'video_os.py doctor' to verify this host."
        ),
    }


def _check(
    group: str,
    name: str,
    ok: bool,
    status: str,
    code: str | None,
    message: str,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "group": group,
        "name": name,
        "ok": bool(ok),
        "status": status,
        "code": code,
        "message": message,
        "action": action,
    }


def _writable_check(path: Path) -> tuple[bool, str]:
    if not path.is_dir():
        return False, f"Directory does not exist: {path}"
    marker = path / f".video-os-doctor-{uuid.uuid4().hex}.tmp"
    try:
        marker.write_bytes(b"doctor")
        marker.unlink()
    except OSError as exc:
        marker.unlink(missing_ok=True)
        return False, f"Directory is not writable: {path}: {exc}"
    return True, f"Writable: {path}"


def doctor(
    *,
    data_root: str | Path | None = None,
    config_path: str | Path | None = None,
    runtime_overrides: Mapping[str, str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    config_file = configured_path(
        config_path=config_path, data_root=data_root, environ=environment
    )
    config = load_system_config(
        config_path=config_file, data_root=data_root, environ=environment
    )
    paths = system_paths(data_root or (config or {}).get("data_root"))
    configured_runtime = (
        config.get("runtime") if isinstance((config or {}).get("runtime"), dict) else {}
    )
    overrides = dict(runtime_overrides or {})
    for name in ("python", "node", "ffmpeg", "ffprobe", "browser"):
        if not overrides.get(name):
            item = configured_runtime.get(name)
            if isinstance(item, dict) and item.get("path"):
                overrides[name] = str(item["path"])
    runtime = discover_runtime(overrides, environ=environment)
    checks: list[dict[str, Any]] = []
    core_ok = (CORE_DIR / "project_manager.py").is_file()
    checks.append(
        _check("Core", "Video OS Core", core_ok, "ready" if core_ok else "missing", None if core_ok else "CORE_MISSING", "Core entrypoints are present" if core_ok else "Video OS Core files are missing")
    )
    config_ok = config is not None
    checks.append(
        _check(
            "Core",
            "Config",
            config_ok,
            "ready" if config_ok else "unconfigured",
            None if config_ok else "CONFIG_NOT_FOUND",
            f"Configuration loaded: {config_file}" if config_ok else f"Configuration not found or invalid: {config_file}",
            None if config_ok else "Run 'video_os.py setup'.",
        )
    )
    code_names = {
        "python": "RUNTIME_PYTHON_MISSING",
        "node": "RUNTIME_NODE_MISSING",
        "ffmpeg": "RUNTIME_FFMPEG_MISSING",
        "ffprobe": "RUNTIME_FFPROBE_MISSING",
        "browser": "RUNTIME_BROWSER_MISSING",
    }
    groups = {"python": "Core", "node": "Perception", "ffmpeg": "Media", "ffprobe": "Media", "browser": "Perception"}
    for name in ("python", "node", "ffmpeg", "ffprobe", "browser"):
        item = runtime["components"][name]
        ready = item["status"] == "ready"
        checks.append(
            _check(
                groups[name],
                name.capitalize(),
                ready,
                item["status"],
                None if ready else code_names[name],
                f"{name} ready: {item.get('path')}" if ready else str((item.get("error") or {}).get("message") or f"{name} unavailable"),
                None if ready else f"Install {name} or configure its explicit path.",
            )
        )

    for label, path in (("Data root", paths["data_root"]), ("Projects root", paths["projects"])):
        writable, message = _writable_check(path)
        checks.append(
            _check("Storage", label, writable, "writable" if writable else "unavailable", None if writable else "STORAGE_NOT_WRITABLE", message, None if writable else "Choose or create a writable Video OS data root.")
        )
    if paths["data_root"].exists():
        try:
            free = shutil.disk_usage(paths["data_root"]).free
            free_ok = free >= 1024 * 1024 * 1024
            checks.append(
                _check(
                    "Storage",
                    "Free space",
                    free_ok,
                    "ready" if free_ok else "low",
                    None if free_ok else "STORAGE_LOW_SPACE",
                    f"Free space: {free / (1024 ** 3):.1f} GiB",
                    None if free_ok else "Free at least 1 GiB before rendering.",
                )
            )
        except OSError as exc:
            checks.append(_check("Storage", "Free space", False, "unknown", "STORAGE_SPACE_UNKNOWN", str(exc)))

    knowledge_path = None
    if config and isinstance(config.get("paths"), dict):
        knowledge_path = config["paths"].get("knowledge_root")
    knowledge_status = inspect_knowledge_root(knowledge_path, environ=environment)
    knowledge_ok = bool(knowledge_status.get("ok"))
    checks.append(
        _check(
            "Core",
            "Knowledge root",
            knowledge_ok,
            str(knowledge_status.get("state") or "unconfigured"),
            None if knowledge_ok else "KNOWLEDGE_ROOT_UNAVAILABLE",
            str(knowledge_status.get("message") or "Knowledge Root unavailable"),
            None if knowledge_ok else "Run setup or initialize the configured Knowledge Root.",
        )
    )

    provider = config.get("provider") if config and isinstance(config.get("provider"), dict) else {"type": "none"}
    provider_type = str(provider.get("type") or "none")
    if provider_type == "gemini-worker":
        worker = worker_manager.worker_status(paths["data_root"])
        worker_ok = bool(worker.get("ok")) and worker.get("login_state") == "ready"
        code = None
        action = None
        if worker.get("login_state") == "needs_login":
            code = "WORKER_NEEDS_LOGIN"
            action = "Run 'video_os.py worker login' and complete sign-in."
        elif not worker.get("ok"):
            code = str(worker.get("code") or "WORKER_UNAVAILABLE").upper().replace(".", "_")
            action = str(worker.get("action") or "Run 'video_os.py worker status'.")
        elif worker.get("login_state") != "ready":
            code = "WORKER_NOT_READY"
            action = "Run 'video_os.py worker login' and then worker status."
        checks.append(
            _check(
                "Perception",
                "Provider: Gemini Worker",
                worker_ok,
                str(worker.get("status") or "unavailable"),
                code,
                f"Worker status={worker.get('status')}, login={worker.get('login_state')}",
                action,
            )
        )
        node_modules: str | None = None
        try:
            _worker_config_path, worker_config = worker_manager.load_worker_config(
                paths["data_root"]
            )
            node_modules = str(worker_config.get("nodeModules") or "") or None
        except worker_manager.WorkerError:
            pass
        playwright = worker_manager.discover_playwright(
            runtime["components"]["node"].get("path"),
            node_modules,
            environ=environment,
        )
        playwright_ok = playwright["status"] == "ready"
        checks.append(
            _check("Perception", "Playwright", playwright_ok, playwright["status"], None if playwright_ok else "RUNTIME_PLAYWRIGHT_MISSING", f"Playwright ready: {playwright.get('path')}" if playwright_ok else str((playwright.get("error") or {}).get("message")), None if playwright_ok else "Install Playwright or configure VIDEO_OS_NODE_MODULES.")
        )
    elif provider_type == "qwen-api":
        try:
            qwen = providers.get_provider(
                provider_type,
                script_dir=CORE_DIR.parent,
                skill_root=CORE_DIR.parents[1],
                options=provider,
                environ=environment,
            )
            health = qwen.healthcheck(live=True)
            provider_ok = bool(health.get("ok"))
            provider_status = str(health.get("status") or "unavailable")
            provider_code = None if provider_ok else str(health.get("code") or "PROVIDER_UNAVAILABLE").upper().replace(".", "_")
            provider_message = (
                f"Qwen API ready: model={health.get('model')}"
                if provider_ok
                else f"Qwen API is not ready: {provider_status}"
            )
        except providers.ProviderError as exc:
            provider_ok = False
            provider_status = "unavailable"
            provider_code = exc.code.upper().replace(".", "_")
            provider_message = str(exc)
        checks.append(
            _check(
                "Perception",
                "Provider: Qwen API",
                provider_ok,
                provider_status,
                provider_code,
                provider_message,
                None if provider_ok else "Verify the API key environment, endpoint, and configured model.",
            )
        )
    else:
        checks.append(
            _check("Perception", "Provider", False, "unconfigured", "PROVIDER_NOT_CONFIGURED", "No Perception Provider is selected", "Run setup with --provider gemini-worker or configure an accepted API Provider.")
        )

    failed = [item for item in checks if not item["ok"]]
    return {
        "schema_version": 1,
        "ok": not failed,
        "result": "READY" if not failed else "ATTENTION",
        "config_path": str(config_file),
        "checks": checks,
        "summary": {"passed": len(checks) - len(failed), "failed": len(failed), "total": len(checks)},
    }


def format_doctor(result: Mapping[str, Any]) -> str:
    lines = ["Video OS Doctor", "=" * 48]
    for item in result.get("checks", []):
        marker = "OK" if item.get("ok") else "X"
        code = f" [{item['code']}]" if item.get("code") else ""
        lines.append(f"[{marker}] {item.get('group')} / {item.get('name')}: {item.get('message')}{code}")
        if item.get("action"):
            lines.append(f"     Fix: {item['action']}")
    lines.append("-" * 48)
    lines.append(f"Result: {result.get('result')}")
    summary = result.get("summary") or {}
    lines.append(f"Checks: {summary.get('passed', 0)}/{summary.get('total', 0)} passed")
    return "\n".join(lines)
