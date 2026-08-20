"""Single source of truth for locating and diagnosing Video OS Knowledge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


KNOWLEDGE_ROOT_ENV = "VIDEO_OS_KNOWLEDGE_ROOT"
KNOWLEDGE_DATA_DIRECTORIES = (
    "edits",
    "repair_log",
    "rule_candidates",
    "editing_rules",
    "reviews",
    "governance_history",
    "good_cases",
    "bad_cases",
    "style_profile",
    "client_preferences",
)
USABLE_STATES = frozenset({"initialized_empty", "ready"})


class KnowledgeRootError(RuntimeError):
    """Raised when a Knowledge consumer cannot use the configured root."""

    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__(str(status.get("message") or "Knowledge Root is unavailable"))


def _configuration(
    explicit: Path | str | None,
    environ: Mapping[str, str] | None,
) -> tuple[str | None, str]:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip(), "cli"
    environment = os.environ if environ is None else environ
    configured = str(environment.get(KNOWLEDGE_ROOT_ENV, "")).strip()
    if configured:
        return configured, "environment"
    return None, "none"


def configured_knowledge_root(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    """Resolve only an explicitly supplied or environment-configured root."""
    raw, source = _configuration(explicit, environ)
    if raw is None:
        raise KnowledgeRootError(
            {
                "ok": False,
                "state": "unconfigured",
                "source": source,
                "path": None,
                "environment_variable": KNOWLEDGE_ROOT_ENV,
                "message": (
                    "Knowledge Root is unconfigured; pass --knowledge-root/--root "
                    f"or set {KNOWLEDGE_ROOT_ENV} to an absolute path"
                ),
            }
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise KnowledgeRootError(
            {
                "ok": False,
                "state": "invalid_configuration",
                "source": source,
                "path": str(path),
                "environment_variable": KNOWLEDGE_ROOT_ENV,
                "message": "Knowledge Root must be an absolute path",
            }
        )
    return path.resolve(), source


def inspect_knowledge_root(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Report configuration and on-disk state without modifying anything."""
    try:
        root, source = configured_knowledge_root(explicit, environ=environ)
    except KnowledgeRootError as exc:
        return dict(exc.status)

    base: dict[str, Any] = {
        "ok": False,
        "source": source,
        "path": str(root),
        "environment_variable": KNOWLEDGE_ROOT_ENV,
    }
    if not root.exists():
        return {
            **base,
            "state": "path_missing",
            "message": f"configured Knowledge Root does not exist: {root}",
        }
    if not root.is_dir():
        return {
            **base,
            "state": "invalid",
            "message": f"configured Knowledge Root is not a directory: {root}",
        }

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {
            **base,
            "state": "uninitialized",
            "message": (
                f"Knowledge Root is not initialized (manifest.json missing): {root}"
            ),
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            **base,
            "state": "invalid",
            "message": f"Knowledge manifest is invalid: {exc}",
        }
    try:
        manifest_schema = (
            int(manifest.get("schema_version", 0))
            if isinstance(manifest, dict)
            else 0
        )
    except (TypeError, ValueError):
        manifest_schema = 0
    if not isinstance(manifest, dict) or manifest_schema != 1:
        return {
            **base,
            "state": "invalid",
            "message": "Knowledge manifest must be an object with schema_version=1",
        }

    counts: dict[str, int] = {}
    for name in KNOWLEDGE_DATA_DIRECTORIES:
        directory = root / name
        counts[name] = (
            len([path for path in directory.glob("*.json") if path.is_file()])
            if directory.is_dir()
            else 0
        )
    total = sum(counts.values())
    state = "ready" if total else "initialized_empty"
    return {
        **base,
        "ok": True,
        "state": state,
        "initialized": True,
        "data_files": total,
        "counts": counts,
        "message": (
            f"Knowledge Root is ready with {total} data file(s)"
            if total
            else "Knowledge Root is initialized and empty"
        ),
    }


def require_knowledge_root(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return a usable initialized root, or fail closed with structured state."""
    status = inspect_knowledge_root(explicit, environ=environ)
    if status.get("state") not in USABLE_STATES:
        raise KnowledgeRootError(status)
    return Path(str(status["path"])).resolve()
