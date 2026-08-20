# Install

## Requirements

- Windows 10/11 for the Public Beta acceptance target.
- Python 3.11 or newer; an embedded Python may be supplied by a desktop package.
- FFmpeg and the matching `ffprobe` executable.
- Node.js 20 or newer plus Playwright and Chrome or Microsoft Edge only when using the
  Gemini Browser Worker.
- A DashScope API key only when using Qwen API Perception.

The Core and Qwen Provider use the Python standard library. The Gemini Browser
Worker additionally needs Playwright. Install the repository-pinned JavaScript
dependency from the Skill directory; browser binaries are intentionally not
downloaded because Video OS uses the detected Chrome or Edge installation:

```powershell
cd produce-seeding-video
npm ci --ignore-scripts
```

`setup` automatically discovers this Skill-local `node_modules`. You may still
use `VIDEO_OS_NODE_MODULES` or `--node-modules` for an external installation.
Optional Jianying export and SFX-stem helpers require packages documented in
`requirements-optional.txt`; they are not needed for the default Core, Qwen,
or Gemini Worker production chain.

Video OS does not require Chrome when Edge is available, and does not require a
browser for Qwen API Perception. Automatic Review is configured separately;
the current Qwen adapter is Perception-only.

## User data

Choose a user-owned data root. Setup never silently selects another drive.

```powershell
cd produce-seeding-video
python scripts/video_os.py setup `
  --data-root "$env:LOCALAPPDATA\VideoOS" `
  --provider gemini-worker
python scripts/video_os.py doctor
```

The root contains `config`, `projects`, `knowledge`, `worker`, `cache`, and
`logs`. Upgrades must preserve config, projects, Knowledge, credentials, and
authenticated browser state. Only cache and logs are disposable.

For Qwen, set `QWEN_API_KEY` in the user environment and choose `qwen_api` plus
a video-capable model. Never write the key to JSON.

## Gemini login

```powershell
python scripts/video_os.py worker login
python scripts/video_os.py worker status
python scripts/video_os.py worker start
```

Complete authentication manually in the isolated browser profile. Video OS
does not automate passwords, verification codes, or account recovery.

## Optional helpers

Only install these when using Jianying draft export, Jianying UI automation,
GIF metadata, or the standalone SFX-stem generator:

```powershell
python -m pip install -r requirements-optional.txt
```

See `THIRD_PARTY_NOTICES.md` for dependency and vendored-source notices.
