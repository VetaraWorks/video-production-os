# Video OS

**A self-improving video production engine for AI agents.**

Media → Perception → Plan → Render → QA → Review → Repair → Verified Experience

[简体中文](README.zh-CN.md) · [Install](docs/INSTALL.md) ·
[Quickstart](docs/QUICKSTART.md) · [Architecture](docs/ARCHITECTURE.md)

Video OS turns a script and local media into a deterministic edit plan and a
validated vertical video. Coding agents operate it through one CLI; the Core
does not depend on Codex or another specific agent.

## Why Video OS

- **Agent-native:** run, status, repair, feedback, diagnostics, and redacted
  reports are available through `video_os.py`.
- **Fail-closed production:** Perception, media decoding, QA, and signature-bound
  Review must be real before FINAL.
- **Self-repairing:** a Review `fix` verdict enters bounded Repair, re-render,
  QA, and a new Review of the new video signature.
- **Verified learning:** only provenance-valid, production-verified evidence can
  enter the human-governed candidate/rule/activation chain. Memory remains
  advisory.
- **Replaceable Perception:** use the isolated Gemini Browser Worker or the Qwen
  API Provider. Neither may bypass the existing Perception contract.

## Public Beta quickstart

```powershell
cd produce-seeding-video
npm ci --ignore-scripts # Gemini Browser Worker only; does not download a browser
python scripts/video_os.py setup --data-root "$env:LOCALAPPDATA\VideoOS" --provider gemini-worker
python scripts/video_os.py doctor
python scripts/video_os.py run C:\path\to\project --to PLAN
python scripts/video_os.py run C:\path\to\project --to FINAL
```

Input media stays immutable. Generated artifacts are written under the project
`output/`, while user configuration, Knowledge, Worker profile, caches, and logs
live under the selected data root.

## Safety boundary

Do not hand-edit state or fabricate Perception, QA, Review, Repair, Production
Evidence, rules, activations, or signatures. See [AGENTS.md](AGENTS.md).

Video OS is licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE)
(`AGPL-3.0-or-later`).
Third-party components remain under their respective licenses; see the Skill's
[third-party notices](produce-seeding-video/THIRD_PARTY_NOTICES.md).
