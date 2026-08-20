"""Gemini Browser Worker adapter; queue ownership remains in its managers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from .base import GEMINI_PROVIDER, ProviderError, ProviderRequest, last_json_object


class GeminiWorkerProvider:
    name = GEMINI_PROVIDER

    def __init__(self, *, script_dir: Path, skill_root: Path, **_kwargs: Any) -> None:
        self.script_dir = Path(script_dir).resolve()
        self.skill_root = Path(skill_root).resolve()

    def healthcheck(self, *, live: bool = False) -> dict[str, Any]:
        return {"ok": True, "provider": self.name, "status": "configured", "live": False,
                "message": "Gemini Worker readiness is reported by worker status/login."}

    def invoke(self, request: ProviderRequest, *, worker_config: Path, node: str,
               runner: Callable[..., Any] = subprocess.run, **_kwargs: Any) -> dict[str, Any]:
        command = [node, str(self.script_dir / "gemini_worker.mjs"), "once", "--config",
                   str(worker_config), "--project", str(request.project_dir), "--kind", request.kind]
        if request.kind == "perception":
            command.extend(("--task-id", request.task_id))
        command.extend(("--fail-closed", "--prepare-script", str(self.script_dir / "prepare_perception.py"),
                        "--skill-root", str(self.skill_root)))
        try:
            completed = runner(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", check=False, timeout=request.timeout_seconds,
                               cwd=str(self.script_dir))
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("provider.timeout", f"{request.kind.capitalize()} Provider timed out after {request.timeout_seconds:g}s") from exc
        except OSError as exc:
            raise ProviderError("provider.start_failed", f"{request.kind.capitalize()} Provider could not start: {exc}") from exc
        result = last_json_object(completed.stdout or "")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown Worker error").strip()
            raise ProviderError("provider.execution_failed", f"{request.kind.capitalize()} Provider failed: {detail[-1200:]}")
        if (not isinstance(result, dict) or result.get("status") != "done"
                or result.get("kind") != request.kind or result.get("taskId") != request.task_id):
            raise ProviderError("provider.result_mismatch", f"{request.kind.capitalize()} Provider returned idle, malformed, or mismatched completion")
        return {"provider": self.name, "kind": request.kind, "task_id": request.task_id,
                "status": "done", "raw": result}
