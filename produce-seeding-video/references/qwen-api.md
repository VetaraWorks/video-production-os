# Qwen API Perception

Qwen is an optional Perception Provider. It does not perform Review and never
falls back automatically to Gemini.

```json
{
  "perception": {
    "provider": "qwen_api",
    "model": "qwen3-vl-flash",
    "api_key_env": "QWEN_API_KEY"
  }
}
```

Set the named environment variable in the user environment. Never place an API
key in project JSON, source defaults, reports, logs, or issue attachments.
`QWEN_BASE_URL` may contain either the OpenAI-compatible `/v1` base URL or the
full `/chat/completions` endpoint.

This implementation follows Alibaba Cloud Model Studio's official
[OpenAI-compatible Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
and [vision/video limits](https://help.aliyun.com/zh/model-studio/vision),
verified 2026-08-20. Local video uses a Base64 `video_url` Data URI. The official
encoded limit is 10 MB and recommends raw local files below about 7 MB, so Video
OS enforces a 7 MiB raw proxy maximum. Larger input fails with
`provider.input.too_large`; this release does not upload user media to OSS or
invent a public URL.

The Provider parses one JSON object and requires every Perception segment field
before admission. The existing durable task, merge, input signature, source
signature, and final `perception.json` validation still run. Missing credentials,
HTTP/auth/model errors, malformed output, incomplete segments, stale signatures,
or contract errors fail closed.
