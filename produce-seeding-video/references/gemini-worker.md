# Gemini PWA perception and Review worker

This Provider adapter is replaceable at deployment time. The stable integration
boundaries are validated `perception/perception.json` and `review/review.json`,
not Gemini's web UI. Default production Perception and automatic Review both
fail closed when no usable Provider is configured.

## Why this is isolated

- Gemini observes proxy video and returns facts; it never writes an edit plan.
- The local pipeline validates every returned source path, source signature,
  duration, safe range, score, and identifier before planning.
- Browser or provider failures cannot corrupt source media or existing plans.
- Provider tasks are resumable through durable queue states.
- Frame-level defects remain local algorithmic QC because a multimodal model can
  miss very short bad frames, rapid motion, or a transition remnant.

## Configure the local worker

Create a dedicated visible Chrome or Microsoft Edge profile and Worker
configuration. The CLI discovers Python, Node.js, Playwright, FFmpeg, ffprobe,
and a supported browser without depending on Codex runtime paths. It selects an
available local CDP port starting at 19222, persists it, and never uses the
browser's normal profile.

```powershell
python scripts/video_os.py worker login
```

The default data root is `%LOCALAPPDATA%\VideoOS`. Set `VIDEO_OS_DATA_ROOT`
or pass `--data-root` to choose another location. Pass `--browser`, `--node`,
`--python`, `--node-modules`, `--ffmpeg`, or `--ffprobe` only when automatic
discovery is insufficient. Complete authentication manually in the dedicated
window if requested. Never script passwords, one-time codes, security prompts,
or account recovery. `scripts/configure_gemini_worker.ps1` remains a compatible
wrapper that also creates desktop shortcuts.

Inspect the persisted browser, port, Worker process, and login state:

```powershell
python scripts/video_os.py worker status
```

Start or stop the continuous Worker. Stop verifies the exact managed Node
process and never terminates the dedicated or normal browser:

```powershell
python scripts/video_os.py worker start
python scripts/video_os.py worker stop
```

The low-level one-task adapter remains available for diagnostics:

```powershell
$workerConfig = Join-Path $env:LOCALAPPDATA "VideoOS\worker\worker-config.json"
node scripts/gemini_worker.mjs once --config $workerConfig `
  --project <project-dir> --kind review --fail-closed
```

The worker launches the configured browser when needed, attaches through the dedicated local
debugging port, uploads only the proxy named by the active task, submits the
versioned perception prompt, captures JSON, validates it locally, and merges
completed source results. A selector ambiguity fails closed as `needs_human`.
The continuous `run` command uses a single-instance lock and scans one directory
level below every configured project root, so new prepared projects placed under
the configured `projects` directory are discovered without editing the worker
code.

## Queue lifecycle

1. Let `video_os.py run` generate timebase-preserving proxies and source-level
   tasks automatically. Use `prepare_perception.py prepare` directly only for
   queue diagnostics or administration.
2. Move one task from `queued` to `running`, then `uploading` immediately before
   the operator or worker selects the exact proxy named in the task.
3. Upload only the proxy belonging to that task. Do not upload customer footage
   during development without explicit user approval for that exact file and
   destination.
4. Use `references/perception-prompt.md` and require JSON-only output.
5. Save the response outside `done`, import it with `import-result`, and let the
   validator move the task to `done` only after the result is valid.
6. Use `needs_login` for authentication expiry, `needs_human` for ambiguous UI or
   provider responses, and `failed` for non-recoverable provider errors.
7. Merge only when all expected source tasks are `done`.

## Quality rule

Never silently substitute a previous result, an unvalidated response, a result
for a different source signature, or a guessed timestamp. A provider failure is
safer than an apparently successful but semantically wrong edit.

DOM selectors and upload automation stay in `scripts/gemini_worker.mjs`; do not
place them in planning or rendering modules. Run `doctor` after a Gemini UI
update before processing customer footage.
