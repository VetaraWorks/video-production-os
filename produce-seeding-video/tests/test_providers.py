from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core import providers  # noqa: E402
from video_os_core.perception_providers.qwen_api import (  # noqa: E402
    MAX_LOCAL_VIDEO_BYTES,
    QwenApiProvider,
)
from video_os_core.perception_providers.base import last_json_object  # noqa: E402


class ProviderBoundaryTests(unittest.TestCase):
    def test_nested_json_parser_returns_complete_outer_object(self) -> None:
        payload = last_json_object('noise {"source":{"segments":[{"quality":{"usable":true}}]}}')
        self.assertIn("source", payload)
        self.assertEqual(len(payload["source"]["segments"]), 1)

    def test_resolution_has_explicit_then_kind_then_shared_precedence(self) -> None:
        environment = {
            "VIDEO_OS_PERCEPTION_PROVIDER": "kind-provider",
            "VIDEO_OS_PROVIDER": "shared-provider",
        }
        self.assertEqual(
            providers.resolve_provider_name(
                "perception", {"provider": "gemini"}, environ=environment
            ),
            providers.GEMINI_PROVIDER,
        )
        self.assertEqual(
            providers.resolve_provider_name("perception", {}, environ=environment),
            "kind-provider",
        )
        self.assertEqual(
            providers.resolve_provider_name(
                "review", {}, environ={"VIDEO_OS_PROVIDER": "gemini-browser"}
            ),
            providers.GEMINI_PROVIDER,
        )

    def test_unknown_and_none_provider_fail_closed(self) -> None:
        for name, code in (("none", "provider.unconfigured"), ("unknown", "provider.unsupported")):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.get_provider(name, script_dir=ROOT / "scripts", skill_root=ROOT)
            self.assertEqual(caught.exception.code, code)

    def test_gemini_adapter_invokes_exact_task_and_returns_structured_result(self) -> None:
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='noise\n{"status":"done","kind":"perception","taskId":"task-1"}\n',
                stderr="",
            )

        provider = providers.GeminiBrowserProvider(
            script_dir=ROOT / "scripts", skill_root=ROOT
        )
        result = provider.invoke(
            providers.ProviderRequest("perception", ROOT, "task-1", 12),
            worker_config=Path("worker.json"),
            node="node",
            runner=runner,
        )
        self.assertEqual(result["provider"], providers.GEMINI_PROVIDER)
        self.assertEqual(result["task_id"], "task-1")
        command = captured["command"]
        self.assertEqual(command[command.index("--task-id") + 1], "task-1")
        self.assertTrue(captured["kwargs"]["check"] is False)

    def test_malformed_completion_is_never_success(self) -> None:
        provider = providers.GeminiBrowserProvider(
            script_dir=ROOT / "scripts", skill_root=ROOT
        )
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"status":"done","kind":"review","taskId":"stale"}', stderr=""
        )
        with self.assertRaises(providers.ProviderError) as caught:
            provider.invoke(
                providers.ProviderRequest("review", ROOT, "current", 1),
                worker_config=Path("worker.json"),
                node="node",
                runner=lambda *_args, **_kwargs: completed,
            )
        self.assertEqual(caught.exception.code, "provider.result_mismatch")


class QwenApiProviderTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def test_healthcheck_never_returns_key_value(self) -> None:
        key = "secret-test-key"
        provider = QwenApiProvider(environ={"QWEN_API_KEY": key})
        health = provider.healthcheck(live=False)
        self.assertTrue(health["ok"])
        self.assertNotIn(key, json.dumps(health))
        self.assertEqual(health["api_key_env"], "QWEN_API_KEY")

    def test_live_healthcheck_verifies_parseable_model_response(self) -> None:
        captured = {}

        def opener(request, **kwargs):
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data)
            return self.Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

        provider = QwenApiProvider(environ={"QWEN_API_KEY": "test-key"}, opener=opener)
        result = provider.healthcheck(live=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(captured["payload"]["model"], provider.model)

    def test_video_request_uses_base64_and_returns_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qwen-provider-") as temporary:
            proxy = Path(temporary) / "proxy.mp4"
            proxy.write_bytes(b"real-proxy-test")
            captured = {}

            def opener(request, **kwargs):
                captured["payload"] = json.loads(request.data)
                content = json.dumps({"source": {"segments": [{
                    "id": "s1", "start": 0, "end": 1, "safe_start": 0,
                    "safe_end": 1, "summary": "pattern", "semantic_tags": [],
                    "subjects": [], "objects": [], "actions": [],
                    "script_alignment": [], "quality": {"usable": True, "score": 1, "issues": []},
                    "confidence": 1, "visual_fingerprint": "pattern",
                }]}})
                return self.Response({"choices": [{"message": {"content": content}}]})

            provider = QwenApiProvider(environ={"QWEN_API_KEY": "test-key"}, opener=opener)
            result = provider.invoke(
                providers.ProviderRequest("perception", Path(temporary), "task-1", 9),
                task={"proxy_path": str(proxy)},
                prompt="contract",
            )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["payload"]["provider"]["name"], providers.QWEN_PROVIDER)
        media = captured["payload"]["messages"][0]["content"][0]
        self.assertTrue(media["video_url"]["url"].startswith("data:video/mp4;base64,"))

    def test_oversized_local_video_fails_before_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qwen-provider-large-") as temporary:
            proxy = Path(temporary) / "proxy.mp4"
            with proxy.open("wb") as handle:
                handle.truncate(MAX_LOCAL_VIDEO_BYTES + 1)
            provider = QwenApiProvider(
                environ={"QWEN_API_KEY": "test-key"},
                opener=lambda *_args, **_kwargs: self.fail("network must not be called"),
            )
            with self.assertRaises(providers.ProviderError) as caught:
                provider.invoke(
                    providers.ProviderRequest("perception", Path(temporary), "task-1", 9),
                    task={"proxy_path": str(proxy)},
                    prompt="contract",
                )
        self.assertEqual(caught.exception.code, "provider.input.too_large")

    def test_parseable_but_incomplete_segment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qwen-provider-invalid-") as temporary:
            proxy = Path(temporary) / "proxy.mp4"
            proxy.write_bytes(b"video")
            response = self.Response({"choices": [{"message": {"content":
                '{"source":{"segments":[{"id":"s1"}]}}'}}]})
            provider = QwenApiProvider(
                environ={"QWEN_API_KEY": "test-key"},
                opener=lambda *_args, **_kwargs: response,
            )
            with self.assertRaises(providers.ProviderError) as caught:
                provider.invoke(
                    providers.ProviderRequest("perception", Path(temporary), "task-1", 9),
                    task={"proxy_path": str(proxy)},
                    prompt="contract",
                )
        self.assertEqual(caught.exception.code, "provider.result.invalid")


if __name__ == "__main__":
    unittest.main()
