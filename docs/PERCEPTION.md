# Perception

## Who watches the video?

One configured external Provider analyzes each Director-prepared proxy. The
Provider returns observable segments; it does not choose the final edit. Video
OS then admits the response through the durable task, merge, source/input
signature, schema, safe-range, and current-project validation chain.

## API Provider versus Browser Worker

- **Qwen API:** sends an eligible Base64 proxy to Alibaba Cloud DashScope over
  HTTPS. It needs an API key environment variable and a video-capable model.
  The Public Beta enforces a 7 MiB raw local proxy limit and does not upload to
  OSS or silently fall back.
- **Gemini Browser Worker:** controls a dedicated Chrome or Edge profile through
  local CDP, uploads the proxy to the logged-in Gemini web application, reads
  JSON, and validates it locally. The user's normal browser profile is never
  reused.

Gemini Browser Worker is **not mandatory**. Qwen API can perform Perception
without a browser. Conversely, Qwen is not an automatic Review Provider in this
release, so FINAL still needs a separately configured Review path.

## Does media leave the machine?

Yes, when either external Provider is used: Gemini uploads the proxy through the
web UI; Qwen sends a Base64 proxy to DashScope. Original media is not submitted
by these adapters, but the proxy still contains user media. Do not enable a
Provider unless the user is authorized to send that content to it.

## Diagnosing credentials

- Gemini: `worker status` reports `needs_login`; run `worker login` and complete
  authentication manually.
- Qwen: Doctor reports missing credentials, HTTP/auth/model failure, or ready.
  It shows only the API-key environment-variable name, never the key value.

Any unavailable, timeout, malformed, incomplete, stale, or signature-mismatched
Provider result fails closed before PLAN.
