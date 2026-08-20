"""Director adapter for the existing Perception queue and Gemini Worker.

This module owns no perception logic. It prepares signature-bound tasks through
prepare_perception.py, invokes the configured Worker for those exact tasks, and
accepts only a fully validated perception.json for the current project inputs.
"""

from __future__ import annotations

import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any

from prepare_perception import (
    QUEUE_STATES,
    admit_perception_result,
    merge,
    prepare,
    transition,
    validate,
)
from video_os_core.runtime import discover_node
from video_os_core import providers


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCRIPT_DIR.parent
# Kept as an optional compatibility hook for embedders/tests. Public builds do
# not assume a developer-machine worker location.
DEFAULT_WORKER_CONFIG: Path | None = None
DEFAULT_TIMEOUT_SECONDS = 300


class PerceptionNeedsHumanError(RuntimeError):
    """Raised when automatic Perception needs operator or Provider action."""


class PerceptionNeedsLoginError(PerceptionNeedsHumanError):
    """Raised when the configured Provider session needs authentication."""


class PerceptionFailedError(RuntimeError):
    """Raised when Perception failed without a safe result to reuse."""


def _options(config: dict[str, Any]) -> dict[str, Any]:
    options = config.get("perception", {})
    return options if isinstance(options, dict) else {}


def automatic_perception_enabled(config: dict[str, Any]) -> bool:
    options = _options(config)
    return bool(options.get("enabled", True) and options.get("auto_run", True))


def perception_required(config: dict[str, Any]) -> bool:
    options = _options(config)
    return bool(options.get("enabled", True) and options.get("required", True))


def _configured_path(value: Any, project_dir: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def resolve_worker_config(project_dir: Path, config: dict[str, Any]) -> Path:
    options = _options(config)
    candidate = _configured_path(options.get("worker_config"), project_dir)
    if candidate is None:
        candidate = _configured_path(
            os.environ.get("VIDEO_OS_PERCEPTION_WORKER_CONFIG"), project_dir
        )
    if candidate is None:
        candidate = _configured_path(
            os.environ.get("VIDEO_OS_REVIEW_WORKER_CONFIG"), project_dir
        )
    if candidate is None:
        candidate = _configured_path(os.environ.get("VIDEO_OS_WORKER_CONFIG"), project_dir)
    if candidate is None and DEFAULT_WORKER_CONFIG is not None and DEFAULT_WORKER_CONFIG.is_file():
        candidate = DEFAULT_WORKER_CONFIG
    if candidate is None or not candidate.is_file():
        raise PerceptionNeedsHumanError(
            "Perception Provider is not configured; set perception.worker_config "
            "or VIDEO_OS_PERCEPTION_WORKER_CONFIG"
        )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerceptionNeedsHumanError(
            f"Perception Worker config is unreadable or invalid: {candidate}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PerceptionNeedsHumanError(
            f"Perception Worker config must be a JSON object: {candidate}"
        )
    return candidate


def _node_executable(project_dir: Path, config: dict[str, Any]) -> str:
    configured = _configured_path(_options(config).get("node"), project_dir)
    discovered = discover_node(str(configured) if configured is not None else None)
    if discovered["status"] != "ready":
        error = discovered.get("error") or {}
        raise PerceptionNeedsHumanError(
            f"Perception Worker requires Node.js [{error.get('code', 'runtime.node.unavailable')}]: "
            f"{error.get('message', 'node was not found')}"
        )
    return str(discovered["path"])


def _task_path(project_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    matches = [
        project_dir / "perception" / "tasks" / state / f"{task_id}.json"
        for state in QUEUE_STATES
        if (project_dir / "perception" / "tasks" / state / f"{task_id}.json").is_file()
    ]
    if len(matches) != 1:
        raise PerceptionNeedsHumanError(
            f"Perception task {task_id} has an ambiguous durable state "
            f"({len(matches)} files)"
        )
    try:
        payload = json.loads(matches[0].read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerceptionNeedsHumanError(
            f"Perception task is unreadable: {matches[0]}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise PerceptionNeedsHumanError(
            f"Perception task identity mismatch: {matches[0]}"
        )
    return matches[0], payload


def _mark_needs_human(project_dir: Path, task_id: str, message: str) -> None:
    try:
        _path, task = _task_path(project_dir, task_id)
        if task.get("status") in {"done", "needs_human"}:
            return
        transition(
            Namespace(
                project_dir=project_dir,
                task_id=task_id,
                state="needs_human",
                error=message[:1000],
                worker_id="video-os-director",
            )
        )
    except Exception:
        return


def _raise_for_task_state(task: dict[str, Any]) -> None:
    status = str(task.get("status") or "")
    detail = str(task.get("error") or "").strip()
    if status == "needs_login":
        raise PerceptionNeedsLoginError(detail or "Perception Provider login is required")
    if status == "needs_human":
        raise PerceptionNeedsHumanError(
            detail or "Perception Provider requires operator action"
        )
    if status == "failed":
        raise PerceptionFailedError(detail or "Perception Provider task failed")


def _run_worker_task(
    project_dir: Path,
    task_id: str,
    worker_config: Path,
    node: str,
    timeout_seconds: float,
    provider: providers.Provider,
    task: dict[str, Any],
) -> None:
    prompt_path = SKILL_ROOT / str(task.get("prompt_contract") or "references/perception-prompt.md")
    try:
        contract = prompt_path.read_text(encoding="utf-8-sig")
        script_path = Path(str(task.get("script_path") or ""))
        script = script_path.read_text(encoding="utf-8-sig") if script_path.is_file() else ""
    except OSError as exc:
        message = f"Perception Provider prompt input is unavailable: {exc}"
        _mark_needs_human(project_dir, task_id, message)
        raise PerceptionNeedsHumanError(message) from exc
    prompt = (
        f"{contract}\n\nCurrent task (authoritative):\n"
        f"source={task.get('source')}\n"
        f"duration={task.get('source_duration')}\n"
        f"signature={json.dumps(task.get('source_signature'), ensure_ascii=True, sort_keys=True)}\n"
        f"script context={script[:12000]}"
    )
    try:
        if provider.name == providers.QWEN_PROVIDER:
            transition(
                Namespace(
                    project_dir=project_dir,
                    task_id=task_id,
                    state="running",
                    error=None,
                    worker_id="qwen-api",
                )
            )
            _path, task = _task_path(project_dir, task_id)
        provider_result = provider.invoke(
            providers.ProviderRequest(
                kind="perception",
                project_dir=project_dir,
                task_id=task_id,
                timeout_seconds=timeout_seconds,
            ),
            worker_config=worker_config,
            node=node,
            runner=subprocess.run,
            task=task,
            prompt=prompt,
        )
        payload = provider_result.get("payload")
        if payload is not None:
            if not isinstance(payload, dict):
                raise providers.ProviderError(
                    "provider.result.invalid", "Provider payload must be an object"
                )
            try:
                admit_perception_result(
                    project_dir,
                    task_id,
                    payload,
                    worker_id=provider.name,
                )
            except Exception as exc:
                raise providers.ProviderError(
                    "provider.result.invalid",
                    f"Provider result failed Perception admission: {exc}",
                ) from exc
    except providers.ProviderError as exc:
        message = f"[{exc.code}] {exc}"
        _mark_needs_human(project_dir, task_id, message)
        raise PerceptionNeedsHumanError(message) from exc

    _path, durable_task = _task_path(project_dir, task_id)
    _raise_for_task_state(durable_task)
    if durable_task.get("status") != "done":
        message = "Perception Provider returned idle, malformed, or mismatched completion"
        _mark_needs_human(project_dir, task_id, message)
        raise PerceptionNeedsHumanError(message)


def validate_perception_artifact(
    project_dir: Path,
    *,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Run the full contract and current-input validation for perception.json."""

    return validate(
        Namespace(project_dir=Path(project_dir).resolve(), ffprobe=ffprobe)
    )


def run_automatic_perception(
    project_dir: Path,
    config: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Prepare, execute, merge, and validate Perception for current inputs."""

    project_dir = Path(project_dir).resolve()
    if not automatic_perception_enabled(config):
        raise PerceptionNeedsHumanError("automatic Perception is disabled")
    options = _options(config)
    work_root = _configured_path(options.get("work_root"), project_dir)
    try:
        timeout_seconds = float(
            options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise PerceptionNeedsHumanError(
            "perception.timeout_seconds must be numeric"
        ) from exc
    if timeout_seconds <= 0:
        raise PerceptionNeedsHumanError(
            "perception.timeout_seconds must be positive"
        )

    try:
        manifest = prepare(
            Namespace(
                project_dir=project_dir,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                work_root=work_root,
                force=False,
            )
        )
    except (FileNotFoundError, OSError) as exc:
        raise PerceptionNeedsHumanError(
            f"Perception task preparation is unavailable: {exc}"
        ) from exc
    except Exception as exc:
        raise PerceptionFailedError(
            f"Perception task preparation failed: {exc}"
        ) from exc
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise PerceptionFailedError(
            "Perception task preparation returned an invalid manifest"
        )

    current: list[tuple[str, dict[str, Any]]] = []
    for item in tasks:
        task_id = str(item.get("task_id") or "") if isinstance(item, dict) else ""
        if not task_id:
            raise PerceptionFailedError("Perception manifest contains an invalid task")
        _path, task = _task_path(project_dir, task_id)
        _raise_for_task_state(task)
        current.append((task_id, task))

    queued = [task_id for task_id, task in current if task.get("status") == "queued"]
    non_runnable = [
        (task_id, str(task.get("status") or ""))
        for task_id, task in current
        if task.get("status") not in {"queued", "done"}
    ]
    if non_runnable:
        task_id, status = non_runnable[0]
        message = f"Perception task {task_id} is in non-runnable state {status!r}"
        _mark_needs_human(project_dir, task_id, message)
        raise PerceptionNeedsHumanError(message)

    reused = not queued
    if queued:
        try:
            provider_name = providers.resolve_provider_name("perception", options)
            provider = providers.get_provider(
                provider_name,
                script_dir=SCRIPT_DIR,
                skill_root=SKILL_ROOT,
                options=options,
            )
        except providers.ProviderError as exc:
            message = f"[{exc.code}] {exc}"
            for task_id in queued:
                _mark_needs_human(project_dir, task_id, message)
            raise PerceptionNeedsHumanError(message) from exc
        worker_config: Path | None = None
        node = ""
        if provider.name == providers.GEMINI_PROVIDER:
            worker_config = resolve_worker_config(project_dir, config)
            node = _node_executable(project_dir, config)
        for task_id in queued:
            _run_worker_task(
                project_dir,
                task_id,
                worker_config or Path(),
                node,
                timeout_seconds,
                provider,
                next(task for current_id, task in current if current_id == task_id),
            )

    try:
        merged = merge(Namespace(project_dir=project_dir, ffprobe=ffprobe))
        validated = validate_perception_artifact(project_dir, ffprobe=ffprobe)
    except (FileNotFoundError, OSError) as exc:
        raise PerceptionNeedsHumanError(
            f"Perception validation is unavailable: {exc}"
        ) from exc
    except Exception as exc:
        raise PerceptionFailedError(
            f"Perception merge or contract validation failed: {exc}"
        ) from exc
    return {
        "status": "done",
        "reused": reused,
        "task_ids": [task_id for task_id, _task in current],
        "merged": merged,
        "validated": validated,
    }
