"""Perception Provider registry."""

from pathlib import Path
from typing import Any, Mapping

from .base import GEMINI_PROVIDER, QWEN_PROVIDER, Provider, ProviderError, ProviderRequest, resolve_provider_name
from .gemini_worker import GeminiWorkerProvider
from .qwen_api import QwenApiProvider

GeminiBrowserProvider = GeminiWorkerProvider


def get_provider(name: str, *, script_dir: Path, skill_root: Path,
                 options: Mapping[str, Any] | None = None,
                 environ: Mapping[str, str] | None = None, **kwargs: Any) -> Provider:
    normalized = str(name or "").strip().lower()
    if normalized in {"", "none", "off"}:
        raise ProviderError("provider.unconfigured", "No Provider is configured")
    if normalized == GEMINI_PROVIDER:
        return GeminiWorkerProvider(script_dir=script_dir, skill_root=skill_root)
    if normalized == QWEN_PROVIDER:
        return QwenApiProvider(options=options, environ=environ, **kwargs)
    raise ProviderError("provider.unsupported", f"Configured Provider is not available: {normalized}")


__all__ = ["GEMINI_PROVIDER", "QWEN_PROVIDER", "GeminiBrowserProvider", "GeminiWorkerProvider", "Provider",
           "ProviderError", "ProviderRequest", "QwenApiProvider", "get_provider",
           "resolve_provider_name"]
