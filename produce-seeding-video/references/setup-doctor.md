# Setup and Doctor

Video OS keeps user data outside the installed Skill. Choose the root
explicitly; Setup never selects another drive without the operator requesting
it.

```powershell
npm ci --ignore-scripts  # Gemini Browser Worker only
python scripts/video_os.py setup `
  --data-root "$env:LOCALAPPDATA\VideoOS" `
  --provider gemini-worker
python scripts/video_os.py doctor
```

The pinned Playwright package is installed into the Skill-local
`node_modules/`, which Setup discovers automatically. The install does not
download a browser; Runtime Discovery uses an existing Chrome or Edge. Qwen
API Perception does not need Node, Playwright, or a browser.

The user root contains `config/`, `projects/`, `knowledge/`, `worker/`,
`cache/`, and `logs/`. Upgrades may replace application files, but must not
replace `config`, projects, Knowledge, authenticated browser state, or
credentials. Cache and logs are the only explicitly cleanable directories.

`config/video-os.json` is versioned and stores paths plus a Provider type. It
may store the *name* of an API-key environment variable, never the key value.
Explicit CLI flags and existing environment variables take precedence over
stored defaults. Setup preserves an existing configuration unless `--force`
is supplied.

Doctor checks Core files, configuration, Python, Node.js, FFmpeg, ffprobe,
Chrome or Edge, writable storage, free space, Knowledge, Provider state,
Playwright, and Gemini login when applicable. It returns a non-zero exit code
for attention items and prints stable codes such as
`RUNTIME_FFMPEG_MISSING` or `WORKER_NEEDS_LOGIN`. Use `--json` for automation.
Doctor does not initialize a project, run a stage, or change project state.
