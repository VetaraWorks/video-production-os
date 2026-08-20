"""Lifecycle management for the optional Gemini Browser Worker.

This module owns only local Worker configuration and processes. It never edits
Video OS project state and never controls a user's normal browser profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import ctypes
from pathlib import Path
from typing import Any, Callable, Mapping

from video_os_core.runtime import discover_runtime


SCRIPT_DIR = Path(__file__).resolve().parents[1]
WORKER_SCRIPT = SCRIPT_DIR / "gemini_worker.mjs"
CDP_PORT_START = 19222
CDP_PORT_END = 19321
DEFAULT_TIMEOUT_SECONDS = 15.0


class WorkerError(RuntimeError):
    """Actionable Worker lifecycle failure with a stable machine code."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _error(component: str, message: str, *, source: str = "none") -> dict[str, Any]:
    return {
        "status": "unavailable",
        "path": None,
        "source": source,
        "version": None,
        "error": {"code": f"runtime.{component}.unavailable", "message": message},
    }


def _ready(path: Path, source: str) -> dict[str, Any]:
    return {
        "status": "ready",
        "path": str(path.resolve()),
        "source": source,
        "version": None,
        "error": None,
    }


def default_data_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser().resolve()
    configured = str(os.environ.get("VIDEO_OS_DATA_ROOT") or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    if os.name == "nt":
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if not local:
            raise WorkerError(
                "worker.data_root_unavailable",
                "LOCALAPPDATA is unavailable; pass --data-root or set VIDEO_OS_DATA_ROOT",
            )
        return (Path(local) / "VideoOS").resolve()
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share") / "video-os"


def worker_paths(data_root: str | Path | None = None) -> dict[str, Path]:
    root = default_data_root(data_root)
    worker = root / "worker"
    return {
        "data_root": root,
        "worker_root": worker,
        "config": worker / "worker-config.json",
        "profile": worker / "browser-profile",
        "logs": worker / "logs",
        "projects": root / "projects",
        "lock": worker / "worker.lock",
        "process": worker / "worker-process.json",
        "browser_session": worker / "browser-session.json",
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _port_available(port: int) -> bool:
    if not 1 <= int(port) <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            candidate.bind(("127.0.0.1", int(port)))
    except OSError:
        return False
    return True


def allocate_cdp_port(
    start: int = CDP_PORT_START,
    end: int = CDP_PORT_END,
    *,
    available: Callable[[int], bool] = _port_available,
) -> int:
    for port in range(int(start), int(end) + 1):
        if available(port):
            return port
    raise WorkerError(
        "worker.cdp_port_unavailable",
        f"No available local CDP port was found in {start}-{end}",
    )


def discover_playwright(
    node_path: str | None,
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    requested = str(explicit or "").strip()
    source = "explicit"
    if not requested:
        requested = str(environment.get("VIDEO_OS_NODE_MODULES") or "").strip()
        source = "environment:VIDEO_OS_NODE_MODULES"
    if requested:
        candidate = Path(os.path.expandvars(requested)).expanduser()
        if (candidate / "playwright" / "package.json").is_file():
            return _ready(candidate, source)
        return _error(
            "playwright",
            f"Configured Node modules do not contain Playwright: {candidate}",
            source=source,
        )

    candidates: list[tuple[str, Path]] = []
    for item in str(environment.get("NODE_PATH") or "").split(os.pathsep):
        if item.strip():
            candidates.append(("environment:NODE_PATH", Path(item).expanduser()))
    candidates.append(("skill_local", SCRIPT_DIR.parent / "node_modules"))
    if node_path:
        node_dir = Path(node_path).resolve().parent
        candidates.extend(
            [
                ("node_sibling", node_dir / "node_modules"),
                ("node_parent", node_dir.parent / "node_modules"),
            ]
        )
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if npm:
        try:
            completed = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            global_root = completed.stdout.strip()
            if completed.returncode == 0 and global_root:
                candidates.append(("npm_global", Path(global_root)))
        except (OSError, subprocess.SubprocessError):
            pass
    seen: set[str] = set()
    for source, candidate in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "playwright" / "package.json").is_file():
            return _ready(candidate, source)
    return _error(
        "playwright",
        "Playwright was not found; set VIDEO_OS_NODE_MODULES or pass --node-modules",
    )


def discover_worker_runtime(
    overrides: Mapping[str, str | None] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    probe_versions: bool = True,
) -> dict[str, Any]:
    values = dict(overrides or {})
    runtime = discover_runtime(
        values, environ=environ, probe_versions=probe_versions
    )
    playwright = discover_playwright(
        runtime["components"]["node"].get("path"),
        values.get("node_modules"),
        environ=environ,
    )
    components = {**runtime["components"], "playwright": playwright}
    return {
        "schema_version": 1,
        "ok": all(item["status"] == "ready" for item in components.values()),
        "components": components,
    }


def load_worker_config(data_root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    paths = worker_paths(data_root)
    path = paths["config"]
    config = _read_json(path)
    if config is None:
        raise WorkerError(
            "worker.not_configured",
            f"Gemini Browser Worker is not configured: {path}. Run 'video_os.py worker login'.",
        )
    required = (
        "userDataDir",
        "remoteDebuggingPort",
        "nodeModules",
        "pythonPath",
        "prepareScript",
        "ffmpegPath",
        "ffprobePath",
    )
    browser = config.get("browserPath") or config.get("chromePath")
    missing = [name for name in required if not config.get(name)]
    if not browser:
        missing.append("browserPath")
    if missing:
        raise WorkerError(
            "worker.config_invalid",
            "Worker configuration is missing required fields: " + ", ".join(sorted(missing)),
        )
    try:
        port = int(config["remoteDebuggingPort"])
    except (TypeError, ValueError) as exc:
        raise WorkerError("worker.config_invalid", "Worker CDP port is invalid") from exc
    if not 1 <= port <= 65535:
        raise WorkerError("worker.config_invalid", "Worker CDP port is out of range")
    if int(config.get("schemaVersion") or 1) >= 2:
        configured_profile = Path(str(config["userDataDir"])).expanduser().resolve()
        if configured_profile != paths["profile"].resolve():
            raise WorkerError(
                "worker.profile_not_isolated",
                "Managed Worker configuration must use the dedicated Video OS browser profile",
            )
    config["browserPath"] = str(browser)
    return path, config


def initialize_worker(
    data_root: str | Path | None = None,
    *,
    overrides: Mapping[str, str | None] | None = None,
    cdp_port: int | None = None,
    force: bool = False,
    probe_versions: bool = True,
) -> dict[str, Any]:
    paths = worker_paths(data_root)
    if paths["config"].is_file() and not force:
        config_path, config = load_worker_config(paths["data_root"])
        return {
            "ok": True,
            "created": False,
            "config_path": str(config_path),
            "config": config,
        }

    runtime = discover_worker_runtime(
        overrides, probe_versions=probe_versions
    )
    if not runtime["ok"]:
        unavailable = {
            name: item.get("error")
            for name, item in runtime["components"].items()
            if item["status"] != "ready"
        }
        raise WorkerError(
            "worker.runtime_incomplete",
            "Gemini Browser Worker runtime is incomplete: " + ", ".join(unavailable),
            details=unavailable,
        )
    if cdp_port is not None:
        port = int(cdp_port)
        if not _port_available(port):
            raise WorkerError(
                "worker.cdp_port_conflict", f"Configured CDP port is already in use: {port}"
            )
    else:
        port = allocate_cdp_port()

    for name in ("worker_root", "profile", "logs", "projects"):
        paths[name].mkdir(parents=True, exist_ok=True)
    components = runtime["components"]
    config: dict[str, Any] = {
        "schemaVersion": 2,
        "workerId": "gemini-browser-worker-01",
        "workerInstanceId": uuid.uuid4().hex,
        "browserPath": components["browser"]["path"],
        "browserType": components["browser"].get("kind"),
        "chromePath": components["browser"]["path"],
        "userDataDir": str(paths["profile"]),
        "remoteDebuggingPort": port,
        "geminiUrl": "https://gemini.google.com/app",
        "nodePath": components["node"]["path"],
        "nodeModules": components["playwright"]["path"],
        "pythonPath": components["python"]["path"],
        "skillRoot": str(SCRIPT_DIR.parent),
        "prepareScript": str(SCRIPT_DIR / "prepare_perception.py"),
        "ffmpegPath": components["ffmpeg"]["path"],
        "ffprobePath": components["ffprobe"]["path"],
        "projectRoots": [str(paths["projects"])],
        "pollSeconds": 10,
        "analysisTimeoutSeconds": 900,
        "logPath": str(paths["logs"] / "worker.jsonl"),
        "lockPath": str(paths["lock"]),
    }
    _atomic_write_json(paths["config"], config)
    return {
        "ok": True,
        "created": True,
        "config_path": str(paths["config"]),
        "config": config,
        "runtime": runtime,
    }


def _json_url(url: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "VideoOS/7.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _cdp_state(port: int) -> dict[str, Any]:
    version = _json_url(f"http://127.0.0.1:{port}/json/version")
    pages = _json_url(f"http://127.0.0.1:{port}/json/list") if version else None
    urls = [str(item.get("url") or "") for item in pages or [] if isinstance(item, dict)]
    return {"ready": isinstance(version, dict), "urls": urls}


def _browser_session_owned(paths: Mapping[str, Path], config: Mapping[str, Any]) -> bool:
    session = _read_json(paths["browser_session"])
    if session is None:
        return False
    try:
        profile_matches = Path(str(session.get("profile") or "")).resolve() == Path(
            str(config["userDataDir"])
        ).resolve()
        port_matches = int(session.get("port") or 0) == int(config["remoteDebuggingPort"])
    except (OSError, TypeError, ValueError):
        return False
    instance = str(config.get("workerInstanceId") or "")
    return bool(
        profile_matches
        and port_matches
        and instance
        and session.get("worker_instance_id") == instance
    )


def _last_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            result = payload
    return result


def _login_state(config_path: Path, config: Mapping[str, Any], cdp: Mapping[str, Any]) -> dict[str, Any]:
    if not cdp.get("ready"):
        return {"state": "not_running", "source": "cdp"}
    urls = [str(url) for url in cdp.get("urls") or []]
    if any("accounts.google.com" in url or "gds.google.com" in url for url in urls):
        return {"state": "needs_login", "source": "cdp_url"}
    command = [
        str(config["nodePath"]) if config.get("nodePath") else "",
        str(WORKER_SCRIPT),
        "status",
        "--config",
        str(config_path),
    ]
    if not command[0]:
        node = shutil.which("node")
        command[0] = node or ""
    if command[0]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            payload = _last_json_object(completed.stdout or "")
            state = str((payload or {}).get("status") or "")
            if state in {"ready", "needs_login", "needs_human"}:
                return {"state": state, "source": "worker_status"}
        except (OSError, subprocess.SubprocessError):
            pass
    if any("gemini.google.com" in url for url in urls):
        return {"state": "unknown", "source": "cdp_url"}
    return {"state": "needs_human", "source": "cdp_url"}


def worker_status(data_root: str | Path | None = None) -> dict[str, Any]:
    paths = worker_paths(data_root)
    if not paths["config"].is_file():
        return {
            "schema_version": 1,
            "ok": False,
            "status": "not_configured",
            "code": "worker.not_configured",
            "data_root": str(paths["data_root"]),
            "config_path": str(paths["config"]),
            "action": "Run 'video_os.py worker login' to initialize the dedicated Worker.",
        }
    try:
        config_path, config = load_worker_config(paths["data_root"])
    except WorkerError as exc:
        return {
            "schema_version": 1,
            "ok": False,
            "status": "invalid_config",
            "code": exc.code,
            "error": str(exc),
            "config_path": str(paths["config"]),
        }
    port = int(config["remoteDebuggingPort"])
    cdp = _cdp_state(port)
    cdp_owned = bool(cdp["ready"] and _browser_session_owned(paths, config))
    lock = _read_json(Path(str(config.get("lockPath") or paths["lock"]))) or {}
    pid = int(lock.get("pid") or 0)
    process = _read_json(paths["process"]) or {}
    process_alive = _pid_alive(pid)
    managed = bool(process_alive and int(process.get("pid") or 0) == pid)
    login = (
        _login_state(config_path, config, cdp)
        if cdp_owned
        else {"state": "not_running", "source": "ownership"}
    )
    if cdp["ready"] and not cdp_owned:
        status = "port_conflict"
        code = "worker.cdp_port_conflict"
    elif not cdp["ready"] and not _port_available(port):
        status = "port_conflict"
        code = "worker.cdp_port_conflict"
    elif login["state"] == "needs_login":
        status = "needs_login"
        code = "worker.needs_login"
    elif login["state"] == "ready":
        status = "ready" if process_alive else "browser_ready"
        code = None
    elif process_alive:
        status = "running"
        code = None
    elif cdp["ready"]:
        status = "browser_open"
        code = None
    else:
        status = "stopped"
        code = None
    return {
        "schema_version": 1,
        "ok": status != "port_conflict",
        "status": status,
        "code": code,
        "data_root": str(paths["data_root"]),
        "config_path": str(config_path),
        "browser": {
            "type": config.get("browserType") or "chromium",
            "path": config["browserPath"],
            "profile": config["userDataDir"],
            "port": port,
            "cdp_ready": bool(cdp["ready"]),
            "session_owned": cdp_owned,
        },
        "process": {"status": "running" if process_alive else "stopped", "pid": pid or None, "managed": managed},
        "login_state": login["state"],
        "action": (
            "Complete Gemini sign-in in the dedicated Worker browser, then run worker status again."
            if login["state"] == "needs_login"
            else None
        ),
    }


def _browser_command(config: Mapping[str, Any]) -> list[str]:
    return [
        str(config["browserPath"]),
        f"--user-data-dir={config['userDataDir']}",
        f"--remote-debugging-port={int(config['remoteDebuggingPort'])}",
        "--profile-directory=Default",
        f"--app={config.get('geminiUrl') or 'https://gemini.google.com/app'}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def worker_login(
    data_root: str | Path | None = None,
    *,
    overrides: Mapping[str, str | None] | None = None,
    cdp_port: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    initialized = initialize_worker(data_root, overrides=overrides, cdp_port=cdp_port)
    config_path = Path(initialized["config_path"])
    config = initialized["config"]
    port = int(config["remoteDebuggingPort"])
    cdp = _cdp_state(port)
    launched = False
    paths = worker_paths(data_root)
    if cdp["ready"] and not _browser_session_owned(paths, config):
        raise WorkerError(
            "worker.cdp_port_conflict",
            f"CDP port {port} is active but is not owned by this dedicated Worker profile",
        )
    if not cdp["ready"]:
        if not _port_available(port):
            raise WorkerError(
                "worker.cdp_port_conflict",
                f"CDP port {port} is occupied by another process",
            )
        command = _browser_command(config)
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        process = subprocess.Popen(
            command,
            cwd=str(Path(config_path).parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        _atomic_write_json(
            paths["browser_session"],
            {
                "schema_version": 1,
                "launcher_pid": process.pid,
                "worker_instance_id": config.get("workerInstanceId"),
                "profile": config["userDataDir"],
                "port": port,
            },
        )
        launched = True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if _cdp_state(port)["ready"]:
                break
            time.sleep(0.25)
    status = worker_status(data_root)
    readiness_deadline = time.monotonic() + max(0.0, float(timeout))
    while (
        status.get("login_state") in {"not_running", "unknown", "needs_human"}
        and time.monotonic() < readiness_deadline
    ):
        time.sleep(0.5)
        status = worker_status(data_root)
    return {**status, "command": "login", "launched": launched}


def worker_start(
    data_root: str | Path | None = None,
    *,
    overrides: Mapping[str, str | None] | None = None,
    cdp_port: int | None = None,
    timeout: float = 6.0,
) -> dict[str, Any]:
    initialized = initialize_worker(data_root, overrides=overrides, cdp_port=cdp_port)
    paths = worker_paths(data_root)
    config_path = Path(initialized["config_path"])
    config = initialized["config"]
    existing = _read_json(Path(str(config.get("lockPath") or paths["lock"]))) or {}
    existing_pid = int(existing.get("pid") or 0)
    if _pid_alive(existing_pid):
        return {**worker_status(data_root), "command": "start", "started": False}
    node_path = str((overrides or {}).get("node") or config.get("nodePath") or "").strip()
    if not node_path:
        runtime = discover_worker_runtime(overrides, probe_versions=False)
        node_component = runtime["components"]["node"]
        if node_component["status"] != "ready":
            raise WorkerError(
                "worker.node_unavailable", node_component["error"]["message"]
            )
        node_path = str(node_component["path"])
    config["nodePath"] = str(Path(node_path).resolve())
    _atomic_write_json(config_path, config)
    command = [node_path, str(WORKER_SCRIPT), "run", "--config", str(config_path)]
    paths["logs"].mkdir(parents=True, exist_ok=True)
    launcher_log = paths["logs"] / "worker-launcher.log"
    with launcher_log.open("ab") as log_handle:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            flags |= (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        process = subprocess.Popen(
            command,
            cwd=str(paths["worker_root"]),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            creationflags=flags,
        )
    _atomic_write_json(
        paths["process"],
        {
            "schema_version": 1,
            "pid": process.pid,
            "node": str(Path(node_path).resolve()),
            "script": str(WORKER_SCRIPT.resolve()),
            "config": str(config_path.resolve()),
            "command_sha256": hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest(),
        },
    )
    deadline = time.monotonic() + max(0.0, float(timeout))
    lock_path = Path(str(config.get("lockPath") or paths["lock"]))
    while time.monotonic() < deadline:
        lock = _read_json(lock_path) or {}
        if int(lock.get("pid") or 0) == process.pid:
            return {**worker_status(data_root), "command": "start", "started": True}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is not None:
        raise WorkerError(
            "worker.start_failed",
            f"Gemini Browser Worker exited during startup; inspect {launcher_log}",
        )
    raise WorkerError(
        "worker.start_timeout",
        f"Worker process started but did not acquire its lock within {timeout:g} seconds",
    )


def _process_command_line(pid: int) -> str | None:
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return None
        script = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\"; "
            "if($p){$p.CommandLine}"
        )
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip() or None
    proc = Path("/proc") / str(pid) / "cmdline"
    try:
        return proc.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return None


def worker_stop(
    data_root: str | Path | None = None,
    *,
    timeout: float = 6.0,
) -> dict[str, Any]:
    paths = worker_paths(data_root)
    config_path, config = load_worker_config(paths["data_root"])
    lock_path = Path(str(config.get("lockPath") or paths["lock"]))
    lock = _read_json(lock_path) or {}
    control = _read_json(paths["process"]) or {}
    pid = int(lock.get("pid") or 0)
    if not _pid_alive(pid):
        for path in (lock_path, paths["process"]):
            path.unlink(missing_ok=True)
        return {**worker_status(data_root), "command": "stop", "stopped": False}
    if int(control.get("pid") or 0) != pid:
        raise WorkerError(
            "worker.unmanaged_process",
            "The active Worker was not started by this Video OS CLI; refusing to terminate it",
        )
    command_line = _process_command_line(pid)
    expected = [str(WORKER_SCRIPT.resolve()), str(config_path.resolve())]
    normalized = os.path.normcase(command_line or "")
    if not command_line or any(os.path.normcase(item) not in normalized for item in expected):
        raise WorkerError(
            "worker.process_identity_mismatch",
            "Worker process identity could not be verified; refusing to terminate it",
        )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        raise WorkerError(
            "worker.stop_timeout", f"Worker process {pid} did not stop within {timeout:g} seconds"
        )
    for path in (lock_path, paths["process"]):
        path.unlink(missing_ok=True)
    return {**worker_status(data_root), "command": "stop", "stopped": True}
