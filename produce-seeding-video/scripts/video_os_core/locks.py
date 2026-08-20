"""Per-project single-instance lock (Phase 2).

The lock is a small JSON file holding the owning PID. A lock whose owner PID is
no longer alive is considered stale and can be taken over, which keeps crash
recovery simple without blocking forever.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOCK_FILENAME = "project_state.lock"


class ProjectLockError(RuntimeError):
    """Raised when the project lock cannot be acquired."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_lock(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class ProjectLock:
    """Exclusive lock for one project directory."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.lock_path = self.project_dir / LOCK_FILENAME
        self._acquired = False

    def acquire(self) -> "ProjectLock":
        if self._acquired:
            return self
        for _attempt in (1, 2):
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                info = _read_lock(self.lock_path)
                pid = info.get("pid") if info else None
                if pid is not None:
                    try:
                        pid = int(pid)
                    except (TypeError, ValueError):
                        pid = None
                if pid == os.getpid():
                    raise ProjectLockError(
                        f"Project is already locked by this process: {self.lock_path}"
                    )
                if pid is not None and _pid_alive(pid):
                    raise ProjectLockError(
                        f"Project is locked by another process (pid {pid}): "
                        f"{self.lock_path}"
                    )
                # Stale lock from a dead process: take it over.
                try:
                    self.lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            try:
                payload = {
                    "pid": os.getpid(),
                    "started_at": _now_iso(),
                    "lock_version": 1,
                }
                os.write(fd, json.dumps(payload).encode("utf-8"))
            finally:
                os.close(fd)
            self._acquired = True
            return self
        raise ProjectLockError(
            f"Could not acquire project lock (stale lock conflict): {self.lock_path}"
        )

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            info = _read_lock(self.lock_path)
            if info and int(info.get("pid", -1)) == os.getpid():
                self.lock_path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            pass
        self._acquired = False

    def __enter__(self) -> "ProjectLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def lock_status(project_dir: Path) -> dict[str, Any]:
    """Read-only lock state; does not acquire anything."""
    lock_path = Path(project_dir).resolve() / LOCK_FILENAME
    info = _read_lock(lock_path)
    if not info:
        return {"locked": False, "pid": None, "started_at": None, "stale": False}
    pid = info.get("pid")
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_int = None
    alive = pid_int is not None and _pid_alive(pid_int)
    return {
        "locked": alive,
        "pid": pid_int,
        "started_at": info.get("started_at"),
        "stale": not alive,
    }


def sleep(seconds: float) -> None:
    time.sleep(seconds)
