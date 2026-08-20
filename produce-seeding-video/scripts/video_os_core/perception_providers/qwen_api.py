"""Qwen video Perception adapter using the official OpenAI-compatible API."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import ProviderError, ProviderRequest, QWEN_PROVIDER, last_json_object

DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen3-vl-flash"
MAX_LOCAL_VIDEO_BYTES = 7 * 1024 * 1024
OFFICIAL_DOC = "https://help.aliyun.com/zh/model-studio/vision"
REQUIRED_SEGMENT_FIELDS = {
    "id", "start", "end", "safe_start", "safe_end", "summary",
    "semantic_tags", "subjects", "objects", "actions", "script_alignment",
    "quality", "confidence", "visual_fingerprint",
}


class QwenApiProvider:
    name = QWEN_PROVIDER

    def __init__(self, *, options: Mapping[str, Any] | None = None,
                 environ: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] | None = None, **_kwargs: Any) -> None:
        self.options = dict(options or {})
        self.environ = os.environ if environ is None else environ
        self.api_key_env = str(self.options.get("api_key_env") or "QWEN_API_KEY")
        self.model = str(self.options.get("model") or DEFAULT_MODEL)
        configured_endpoint = str(
            self.options.get("endpoint") or self.environ.get("QWEN_BASE_URL") or DEFAULT_ENDPOINT
        ).rstrip("/")
        self.endpoint = (
            configured_endpoint
            if configured_endpoint.endswith("/chat/completions")
            else configured_endpoint + "/chat/completions"
        )
        if not self.endpoint.lower().startswith("https://"):
            raise ProviderError(
                "provider.endpoint.invalid", "Qwen API endpoint must use HTTPS"
            )
        self._opener = opener or urllib.request.urlopen

    def _api_key(self) -> str:
        value = str(self.environ.get(self.api_key_env) or "").strip()
        if not value:
            raise ProviderError("provider.auth.missing", f"Qwen API key environment variable is missing: {self.api_key_env}")
        return value

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        api_key = self._api_key()
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            response = self._opener(request, timeout=timeout)
            raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read(1200).decode("utf-8", errors="replace").replace(api_key, "[REDACTED]")
            raise ProviderError("provider.http_error", f"Qwen API returned HTTP {exc.code}: {body}", details={"http_status": exc.code}) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("provider.unavailable", f"Qwen API is unavailable: {exc}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider.response.invalid", "Qwen API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderError("provider.response.invalid", "Qwen API response must be an object")
        return decoded

    @staticmethod
    def _message_text(response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("provider.response.invalid", "Qwen API response has no choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        raise ProviderError("provider.response.invalid", "Qwen API response has no message content")

    def healthcheck(self, *, live: bool = False) -> dict[str, Any]:
        configured = bool(str(self.environ.get(self.api_key_env) or "").strip())
        common = {"provider": self.name, "api_key_env": self.api_key_env,
                  "model": self.model, "endpoint": self.endpoint}
        if not configured:
            return {"ok": False, "status": "missing_credentials", "code": "provider.auth.missing", **common}
        if not live:
            return {"ok": True, "status": "configured", "live": False, **common}
        response = self._post({"model": self.model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Return one JSON object: {\"ok\":true}"}]}], "temperature": 0}, 30)
        parsed = last_json_object(self._message_text(response))
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise ProviderError("provider.health.invalid", "Qwen healthcheck was not parseable")
        return {"ok": True, "status": "ready", "live": True, **common}

    def invoke(self, request: ProviderRequest, *, task: Mapping[str, Any], prompt: str,
               **_kwargs: Any) -> dict[str, Any]:
        if request.kind != "perception":
            raise ProviderError("provider.kind.unsupported", "Qwen API is enabled only for Perception")
        proxy_path = Path(str(task.get("proxy_path") or "")).expanduser().resolve()
        if not proxy_path.is_file():
            raise ProviderError("provider.input.missing", f"Perception proxy is missing: {proxy_path}")
        size = proxy_path.stat().st_size
        if size <= 0 or size > MAX_LOCAL_VIDEO_BYTES:
            raise ProviderError("provider.input.too_large",
                "Qwen local Base64 video input must be non-empty and at most 7 MiB; configure a smaller proxy or an approved public-URL transport",
                details={"size_bytes": size, "limit_bytes": MAX_LOCAL_VIDEO_BYTES})
        encoded = base64.b64encode(proxy_path.read_bytes()).decode("ascii")
        payload = {"model": self.model, "messages": [{"role": "user", "content": [
            {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{encoded}", "fps": float(self.options.get("fps") or 1.0)}},
            {"type": "text", "text": prompt}]}], "temperature": 0}
        response = self._post(payload, request.timeout_seconds)
        parsed = last_json_object(self._message_text(response))
        if not isinstance(parsed, dict):
            raise ProviderError("provider.result.invalid", "Qwen API did not return a parseable JSON object")
        source = parsed.get("source")
        segments = source.get("segments") if isinstance(source, dict) else None
        if not isinstance(segments, list) or not segments:
            raise ProviderError("provider.result.invalid", "Qwen API result has no Perception segments")
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ProviderError("provider.result.invalid", f"Qwen segment {index} is not an object")
            missing = sorted(REQUIRED_SEGMENT_FIELDS - set(segment))
            quality = segment.get("quality")
            if missing or not isinstance(quality, dict) or not {"usable", "score", "issues"}.issubset(quality):
                detail = ", ".join(missing) if missing else "quality"
                raise ProviderError("provider.result.invalid", f"Qwen segment {index} is incomplete: {detail}")
        parsed["provider"] = {"name": self.name, "model": self.model}
        return {"provider": self.name, "kind": request.kind, "task_id": request.task_id,
                "status": "done", "payload": parsed}
