"""Thin Director adapter for the existing Review task and Gemini Worker.

This module does not review media itself. It prepares the existing durable
Review task, asks the configured Worker to process that exact task, and fails
closed unless the validated result is bound to the current rendered file.
"""

from __future__ import annotations

import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any

from prepare_perception import (  # Existing Review queue/provider boundary.
    REVIEW_QUEUE_STATES,
    prepare_review,
    transition_review,
)
from video_os_core.runtime import discover_node
from video_os_core import providers


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCRIPT_DIR.parent
# Kept as an optional compatibility hook for embedders/tests. Public builds do
# not assume a developer-machine worker location.
DEFAULT_WORKER_CONFIG: Path | None = None
DEFAULT_TIMEOUT_SECONDS = 300


class ReviewNeedsHumanError(RuntimeError):
    """Raised when a trustworthy automatic Review cannot be obtained."""


def _review_options(config: dict[str, Any]) -> dict[str, Any]:
    video_os = config.get("video_os", {})
    options = video_os.get("review", {}) if isinstance(video_os, dict) else {}
    return options if isinstance(options, dict) else {}


def automatic_review_enabled(config: dict[str, Any]) -> bool:
    return bool(_review_options(config).get("enabled", True))


def _configured_path(value: Any, project_dir: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def resolve_worker_config(project_dir: Path, config: dict[str, Any]) -> Path:
    options = _review_options(config)
    candidate = _configured_path(options.get("worker_config"), project_dir)
    if candidate is None:
        candidate = _configured_path(
            os.environ.get("VIDEO_OS_REVIEW_WORKER_CONFIG"), project_dir
        )
    if candidate is None:
        candidate = _configured_path(os.environ.get("VIDEO_OS_WORKER_CONFIG"), project_dir)
    if candidate is None and DEFAULT_WORKER_CONFIG is not None and DEFAULT_WORKER_CONFIG.is_file():
        candidate = DEFAULT_WORKER_CONFIG
    if candidate is None or not candidate.is_file():
        raise ReviewNeedsHumanError(
            "Review Provider is not configured; set "
            "video_os.review.worker_config or VIDEO_OS_REVIEW_WORKER_CONFIG"
        )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewNeedsHumanError(
            f"Review Worker config is unreadable or invalid: {candidate}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewNeedsHumanError(f"Review Worker config must be a JSON object: {candidate}")
    return candidate


def _node_executable(project_dir: Path, config: dict[str, Any]) -> str:
    configured = _configured_path(_review_options(config).get("node"), project_dir)
    discovered = discover_node(str(configured) if configured is not None else None)
    if discovered["status"] != "ready":
        error = discovered.get("error") or {}
        raise ReviewNeedsHumanError(
            f"Review Worker requires Node.js [{error.get('code', 'runtime.node.unavailable')}]: "
            f"{error.get('message', 'node was not found')}"
        )
    return str(discovered["path"])


def _task_path(project_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    matches = [
        project_dir / "review" / "tasks" / state / f"{task_id}.json"
        for state in REVIEW_QUEUE_STATES
        if (project_dir / "review" / "tasks" / state / f"{task_id}.json").is_file()
    ]
    if len(matches) != 1:
        raise ReviewNeedsHumanError(
            f"Review task {task_id} has an ambiguous durable state ({len(matches)} files)"
        )
    try:
        payload = json.loads(matches[0].read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewNeedsHumanError(f"Review task is unreadable: {matches[0]}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise ReviewNeedsHumanError(f"Review task identity mismatch: {matches[0]}")
    return matches[0], payload


def _mark_needs_human(project_dir: Path, task_id: str, message: str) -> None:
    try:
        _path, task = _task_path(project_dir, task_id)
        if task.get("status") == "needs_human":
            return
        if task.get("status") == "done":
            return
        transition_review(
            Namespace(
                project_dir=project_dir,
                task_id=task_id,
                state="needs_human",
                error=message[:1000],
                worker_id="video-os-director",
            )
        )
    except Exception:
        # Preserve the original Provider failure; PM will still fail closed.
        return


def _verify_provider_result(
    project_dir: Path,
    task: dict[str, Any],
) -> dict[str, Any]:
    review_path = project_dir / "review" / "review.json"
    try:
        review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewNeedsHumanError(
            f"Review Provider did not produce a readable review.json: {exc}"
        ) from exc
    if not isinstance(review, dict):
        raise ReviewNeedsHumanError("Review Provider result must be a JSON object")
    if review.get("status") != "done" or review.get("verdict") not in {"pass", "fix"}:
        raise ReviewNeedsHumanError("Review Provider result has no validated pass/fix verdict")
    target = review.get("target")
    if not isinstance(target, dict) or target.get("signature") != task.get("target_signature"):
        raise ReviewNeedsHumanError("Review Provider result is stale or targets another video")
    if not isinstance(review.get("issues"), list):
        raise ReviewNeedsHumanError("Review Provider result issues must be an array")
    return review


def run_automatic_review(
    project_dir: Path,
    config: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Run the existing Review Provider for the current final video, once."""
    project_dir = Path(project_dir).resolve()
    if not automatic_review_enabled(config):
        raise ReviewNeedsHumanError("automatic Review is disabled")

    options = _review_options(config)
    work_root = _configured_path(options.get("work_root"), project_dir)
    try:
        timeout_seconds = float(options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ReviewNeedsHumanError("video_os.review.timeout_seconds must be numeric") from exc
    if timeout_seconds <= 0:
        raise ReviewNeedsHumanError("video_os.review.timeout_seconds must be positive")

    try:
        manifest = prepare_review(
            Namespace(
                project_dir=project_dir,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                work_root=work_root,
                force=False,
            )
        )
    except Exception as exc:
        raise ReviewNeedsHumanError(f"Review task preparation failed: {exc}") from exc

    task = manifest.get("task") if isinstance(manifest, dict) else None
    if not isinstance(task, dict) or not str(task.get("task_id") or ""):
        raise ReviewNeedsHumanError("Review task preparation returned an invalid manifest")
    task_id = str(task["task_id"])
    status = str(task.get("status") or "")
    if status == "done":
        return {
            "task_id": task_id,
            "status": "done",
            "review": _verify_provider_result(project_dir, task),
            "reused": True,
        }
    if status != "queued":
        message = f"Review task {task_id} is in non-runnable state {status!r}"
        _mark_needs_human(project_dir, task_id, message)
        raise ReviewNeedsHumanError(message)

    try:
        provider_name = providers.resolve_provider_name("review", options)
        provider = providers.get_provider(
            provider_name,
            script_dir=SCRIPT_DIR,
            skill_root=SKILL_ROOT,
            options=options,
        )
        if provider.name == providers.QWEN_PROVIDER:
            raise providers.ProviderError(
                "provider.kind.unsupported",
                "Qwen API is configured for Perception only; Review requires its own Provider",
            )
        worker_config = Path()
        node = ""
        if provider.name == providers.GEMINI_PROVIDER:
            worker_config = resolve_worker_config(project_dir, config)
            node = _node_executable(project_dir, config)
        provider.invoke(
            providers.ProviderRequest(
                kind="review",
                project_dir=project_dir,
                task_id=task_id,
                timeout_seconds=timeout_seconds,
            ),
            worker_config=worker_config,
            node=node,
            runner=subprocess.run,
        )
    except providers.ProviderError as exc:
        message = f"[{exc.code}] {exc}"
        _mark_needs_human(project_dir, task_id, message)
        raise ReviewNeedsHumanError(message) from exc

    _path, durable_task = _task_path(project_dir, task_id)
    if durable_task.get("status") != "done":
        message = f"Review task did not reach done (state={durable_task.get('status')!r})"
        _mark_needs_human(project_dir, task_id, message)
        raise ReviewNeedsHumanError(message)
    return {
        "task_id": task_id,
        "status": "done",
        "review": _verify_provider_result(project_dir, durable_task),
        "reused": False,
    }
