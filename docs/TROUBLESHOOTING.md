# Troubleshooting

Start with:

```powershell
python produce-seeding-video/scripts/video_os.py doctor
```

Common results:

- `RUNTIME_FFMPEG_MISSING`: install FFmpeg or configure its explicit path.
- `RUNTIME_FFPROBE_MISSING`: configure the real `ffprobe` executable, not
  `ffmpeg.exe`.
- `RUNTIME_NODE_MISSING` / `RUNTIME_PLAYWRIGHT_MISSING`: required for Gemini
  Worker only.
- `RUNTIME_BROWSER_MISSING`: install Chrome or Edge, or use API Perception.
- `WORKER_NEEDS_LOGIN`: run `worker login` and authenticate in the isolated
  profile.
- `PROVIDER_AUTH_MISSING`: set the configured API-key environment variable.
- `PROVIDER_INPUT_TOO_LARGE`: Qwen Base64 proxy exceeds 7 MiB; make an approved
  smaller proxy. This release does not auto-upload it elsewhere.

Generate a redacted support archive with `video_os.py report <project>`. It does
not include media, `.env`, browser profile, Cookies, complete Prompt text, or
credential values.
