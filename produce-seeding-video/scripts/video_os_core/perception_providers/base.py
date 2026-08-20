"""Provider interfaces and fail-closed shared parsing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

SUPPORTED_KINDS = {"perception", "review"}
GEMINI_PROVIDER = "gemini-worker"
QWEN_PROVIDER = "qwen-api"


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class ProviderRequest:
    kind: str
    project_dir: Path
    task_id: str
    timeout_seconds: float


class Provider(Protocol):
    name: str

    def healthcheck(self, *, live: bool = False) -> dict[str, Any]: ...

    def invoke(self, request: ProviderRequest, **kwargs: Any) -> dict[str, Any]: ...


def last_json_object(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    longest = -1
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            payload, end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and end > longest:
            result = payload
            longest = end
    return result


def resolve_provider_name(
    kind: str,
    options: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    if kind not in SUPPORTED_KINDS:
        raise ProviderError("provider.kind.invalid", f"Unsupported Provider task kind: {kind}")
    environment = os.environ if environ is None else environ
    configured = str((options or {}).get("provider") or "").strip()
    if not configured:
        configured = str(environment.get(f"VIDEO_OS_{kind.upper()}_PROVIDER") or "").strip()
    if not configured:
        configured = str(environment.get("VIDEO_OS_PROVIDER") or "").strip()
    if not configured:
        configured = GEMINI_PROVIDER
    aliases = {
        "gemini": GEMINI_PROVIDER,
        "gemini-browser": GEMINI_PROVIDER,
        "qwen": QWEN_PROVIDER,
        "qwen_api": QWEN_PROVIDER,
    }
    return aliases.get(configured.lower(), configured.lower())
