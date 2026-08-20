---
name: produce-seeding-video
description: Automatically analyze a product script and local media, create a deterministic edit plan, generate Douyin-style ASS subtitles with highlighted keywords, add restrained hook/product/CTA sound effects, duck optional BGM under speech, and render a validated 9:16 talking-head product recommendation or paid social video with FFmpeg. Use for 口播种草、好物种草、电商广告、真人口播、产品介绍、信息流视频, or when a user provides a project folder containing script/script.txt plus raw_video/ or material/ footage and wants final.mp4, analysis.json, and edit_plan.json. Also use for fixed-template batch production, dry-run planning, subtitle/audio styling, or diagnosing this workflow's media and rendering failures.
---

# Produce Seeding Video

Build a repeatable fixed-template vertical video from a script and local media. Keep creative reasoning in the Skill workflow and deterministic media work in the bundled Python/FFmpeg pipeline.

Use Video OS as the default execution entry. The supported default chain is `SKILL.md -> scripts/video_os.py -> video_os_core/project_manager.py -> video_pipeline`. Treat `scripts/run_pipeline.py` as the internal deterministic backend and explicit low-level debugging interface, not as the normal Skill entry.

For a new host, create the versioned user configuration outside the Skill and
then diagnose it before running a project:

```bash
npm ci --ignore-scripts  # Gemini Browser Worker only; uses detected Chrome/Edge
python scripts/video_os.py setup --data-root <user-data-root> --provider gemini-worker
python scripts/video_os.py doctor
```

Setup preserves existing user configuration and project/Knowledge assets by
default. Doctor is diagnostic only and never edits project state. See
[references/setup-doctor.md](references/setup-doctor.md).
The Qwen API Provider does not require Node or Playwright. Optional helper
dependencies and third-party notices are listed in `requirements-optional.txt`
and `THIRD_PARTY_NOTICES.md`.

## Workflow

1. Inspect the project without modifying source media.
2. Read [references/input-contract.md](references/input-contract.md) and verify the required script and at least one video.
3. Run the default Video OS planning pass. For production tasks that require material understanding, the Director automatically prepares signature-bound Perception tasks, invokes the configured Provider, validates the merged contract, and stops at `PERCEPTION` on Provider or contract failure. Do not manually pre-run the Worker as part of the normal path.
4. Plan through the Director:

   ```bash
   python scripts/video_os.py run <project-dir> --to PLAN
   ```

5. Inspect `<project-dir>/output/analysis.json` and `edit_plan.json`.
6. Check segment intent, source selection, crop safety, subtitle text, warnings, and CTA placement. Adjust `<project-dir>/config/config.json` when deterministic overrides are enough. When a reviewed project needs exact source/timeline decisions, place the complete reviewed plan at `<project-dir>/config/edit_plan.json`; the pipeline will use it instead of regenerating a heuristic plan.
7. Render:

   ```bash
   python scripts/video_os.py run <project-dir> --to FINAL
   ```

8. Require `qa_report.json` to report `"ok": true` before Review. The default Video OS flow then runs the configured automatic Review Provider: `pass` may advance to FINAL, while `fix` must go through REPAIR, RENDER, QA, and a new signature-bound Review. Provider unavailability, timeout, or invalid output is `needs_human`, never success. Extract representative frames and inspect subtitle position, outline, keyword emphasis, safe-area clearance, and CTA treatment. Inspect the soundtrack when playback is available.
9. Return `final.mp4`, `analysis.json`, `edit_plan.json`, `subtitles.ass`, `subtitles.srt`, and `qa_report.json`. When the user wants manual refinement, also export an editable Jianying Pro draft with separate primary-video, B-roll, sound-effect, subtitle, flower-text, and BGM tracks. Report warnings and any fallbacks used.

Run each child project independently for batch jobs. Continue past a failed item, retain its logs, and summarize successes and failures at the end.

For low-level diagnostics or explicit queue administration, prepare low-bitrate,
timebase-preserving proxy videos and durable perception tasks with:

```bash
python scripts/prepare_perception.py prepare <project-dir> --work-root <large-work-drive>
```

Use a large non-system drive for `--work-root` when available. This command never edits source media. It is not required before `video_os.py run`; the Director uses the same preparation boundary automatically. Inspect queue state with `prepare_perception.py status`, and validate a completed `perception/perception.json` with `prepare_perception.py validate` when diagnosing a blocked project.

Use `prepare_perception.py transition` for durable worker state changes. Save each provider response as the JSON shape in [references/perception-prompt.md](references/perception-prompt.md), import it with `prepare_perception.py import-result`, then merge completed source results with `prepare_perception.py merge`. Never mark a task `done` before its result is present and schema-valid.

For the Gemini PWA Provider adapter, read [references/gemini-worker.md](references/gemini-worker.md). Automatic Review is enabled by default; deployments may choose another validated Provider or explicitly disable the gate, but a missing/unavailable configured Provider must not be treated as a pass. Configure a dedicated visible Chrome profile on the large work drive with `scripts/configure_gemini_worker.ps1`, verify it with `scripts/gemini_worker.mjs doctor`, and process durable tasks with `once` or `run`. Require the operator to complete authentication; never automate passwords, verification codes, or account recovery. Keep the queue/provider adapter isolated from planning and rendering so browser breakage cannot invalidate an existing project.

For Qwen API Perception, configure `perception.provider=qwen_api`, a verified
video-capable model, and only the API-key environment-variable name. Qwen API
does not replace automatic Review. Local Base64 proxy input is fail-closed above
the documented size limit; there is no silent Gemini fallback. Read
[references/qwen-api.md](references/qwen-api.md).

For batch execution, place project folders under one root and run each child project independently through Video OS:

```bash
python scripts/video_os.py run <child-project-dir> --to FINAL
```

Use `--to PLAN` for every child when plans must be reviewed before rendering. Continue past a failed child, retain its Video OS state and logs, and summarize successes and failures at the end. `scripts/run_batch.py` remains available only for callers that require the legacy `batch_report.json` compatibility interface; it is not the default Skill execution path.

For an editable Jianying Pro project, complete the normal Video OS render first, then export the reviewed plan without rerunning or bypassing the Video OS production flow:

```bash
python scripts/video_os.py run <project-dir> --to FINAL
python scripts/export_jianying.py <project-dir>/output/edit_plan.json \
  --project-dir <project-dir> \
  --output-dir <project-dir>/output \
  --draft-root "<jianying-draft-root>" \
  --draft-name "项目名-Codex可编辑工程"
```

The export copies referenced media by default. Add `--no-portable-media` to reference original media, and `--zip <path>` to create a portable backup package. A Jianying draft is a directory containing `draft_content.json` and `draft_meta_info.json`, not a rendered MP4. When its parent is Jianying's configured draft location, refresh or reopen Jianying to see it in the project list.

When adding sound effects to a Jianying project that the user has already edited, preserve the user's latest save. Duplicate the complete draft first and modify only the duplicate. For Jianying 6+ encrypted drafts, require a verified decrypt-edit-re-encrypt round trip and keep the original encrypted JSON as a recovery file. Prefer one absolute-time aligned SFX stem on its own named audio track; this preserves exact cue placement while leaving music, speech, video, subtitles, and flower text independently editable. `scripts/generate_sfx_stem.py` builds deterministic, copyright-safe action and flower-text effects. `scripts/inject_jianying_sfx.py` appends that stem to already-decrypted content/meta JSON without rewriting existing tracks.

## Project contract

Require:

```text
project/
├── script/script.txt
├── raw_video/        # talking-head or primary footage
├── material/         # optional product/B-roll/BGM assets
├── reference/        # optional; analyzed but not copied into V1
├── perception/       # Director-managed perception.json and durable worker state
└── config/config.json
```

Treat `raw_video/` and `material/` as immutable inputs. Write all generated artifacts under `output/` unless the user explicitly selects another directory.

Use UTF-8 or UTF-8 with BOM for the script and JSON. Accept common FFmpeg video and audio containers. Read [references/input-contract.md](references/input-contract.md) for filenames, role tagging, and configuration overrides.

## Fixed template

Use `fixed-60s-v1` by default:

- 0–3 seconds: hook with prominent product/person.
- 3–15 seconds: talking-head explanation.
- 15–30 seconds: product and detail shots.
- 30–45 seconds: proof, result, or benefit reinforcement.
- 45–60 seconds: CTA and purchase guidance.

Read [references/template-v1.md](references/template-v1.md) before changing segment timing or role preferences.

## Planning and rendering rules

- Keep `analysis.json` descriptive and `edit_plan.json` executable.
- Preserve the edit-plan schema in [references/edit-plan-schema.md](references/edit-plan-schema.md).
- For default production tasks, require validated current-input Perception facts and safe ranges before planning. Metadata fallback is allowed only when project configuration explicitly declares that material understanding is not required.
- For talking-head videos with full-screen B-roll, use a continuous voice backbone: keep the primary talking-head audio uninterrupted while replacing only the visual layer. Never concatenate B-roll source audio into the narration track.
- Treat external video-perception timestamps as candidate ranges, not frame-accurate edit points. Verify every selected range locally against the original media, trim away setup/exit frames, and record locally verified `safe_start` and `safe_end` values in the reviewed plan.
- When Whisper word timestamps are available, align the exact approved script with `scripts/build_speech_timeline.py` and save `<project-dir>/speech_timeline.json`. The pipeline must prefer these timed cues over sentence-length heuristics.
- Keep the full-screen B-roll source muted, use each source interval once unless repetition is explicitly intentional, and align each event to the current spoken semantic section.
- Treat repeated visual fingerprints or overlapping source ranges as duplicate footage unless the user explicitly requests repetition.
- Keep full-video sparse review out of the normal planning loop. Use local algorithms for frame-level faults and inspect only flagged intervals at high density.
- Loop short clips only when necessary to fill a segment; record this in the plan.
- Normalize every segment to the configured canvas, frame rate, H.264 video, AAC stereo audio, and yuv420p.
- Generate silence for clips without audio so concatenation remains stable.
- Generate short-phrase ASS cues plus an SRT compatibility file from `script.txt`. When word-level timestamps are unavailable, allocate cue time by phrase length and disclose heuristic timing.
- Use the `social-bold` subtitle preset by default: large white bold type, dark outline, lower-middle placement, short cues, and pale-yellow emphasis for numbers, offer language, and configured selling-point keywords.
- Treat `[[text]]` in `script.txt` as explicit highlight markup. Do not show the brackets in the rendered caption.
- Keep sound effects sparse and semantic: accent important flower-text entrances and meaningful visual actions, not every subtitle cue. Prefer action-specific sounds such as bottle shake, creamy texture, foam, massage ticks, proof stamp, result chime, and CTA hit over repeating generic pop/whoosh/ding sounds. Derive cue times from the current editable timeline, especially actual flower-text starts and B-roll cut points.
- Mix BGM only when an audio asset is explicitly configured or clearly tagged as BGM. Duck it under speech and normalize the final mix to the configured loudness/true-peak targets.
- Read [references/subtitle-audio-style.md](references/subtitle-audio-style.md) before changing subtitle styling, highlight behavior, BGM ducking, or sound-effect timing.
- Never invent compliance claims, efficacy claims, prices, guarantees, or product facts that are absent from user inputs.
- Do not use files in `reference/` as output footage unless the user explicitly authorizes it.

## Failure handling

- Stop before rendering when the script is empty, no video is available, ffprobe fails, or template segments are invalid.
- Stop at `PERCEPTION` when its Provider is unconfigured/unavailable, times out, returns invalid data, or produces a result for stale project inputs. Never continue to PLAN through metadata fallback for a default production task.
- Keep planning artifacts when rendering fails so the failure is diagnosable.
- Report the exact failed stage and command summary without exposing secrets or unrelated paths.
- Treat missing BGM, missing reference video, missing clip audio, and insufficient unique footage as recoverable warnings.
- Treat duration, resolution, missing video/audio stream, or zero-byte output failures as QA blockers.

## Resources

- Run `scripts/video_os.py run <project-dir> --to PLAN|FINAL` for the default end-to-end workflow.
- Run `scripts/video_os.py setup` once per user installation and `scripts/video_os.py doctor` for actionable host diagnostics.
- Keep `scripts/run_pipeline.py` as the deterministic backend invoked by Video OS and as an explicit low-level debugging interface only.
- Keep `scripts/run_batch.py` as the legacy batch-report compatibility interface only; default batch execution runs each child through Video OS.
- Run `scripts/prepare_perception.py` to generate proxy media, durable worker tasks, inspect queue state, and validate completed perception output.
- Run `scripts/video_os.py worker login|status|start|stop` to initialize and manage the dedicated Gemini Browser Worker; `scripts/configure_gemini_worker.ps1` remains an optional desktop-shortcut wrapper.
- Keep `scripts/launch_gemini_pwa.ps1` only as the low-level profile bootstrap fallback.
- Run `scripts/generate_sfx_assets.py` to deterministically rebuild the bundled copyright-safe sound effects.
- Run `scripts/export_jianying.py` to export an already-reviewed standard or fullscreen edit plan as a native editable Jianying draft.
- Read `references/input-contract.md` when preparing a project.
- Read `references/edit-plan-schema.md` when generating or modifying plans.
- Read `references/perception-contract.md` when preparing, validating, or consuming external video-understanding results.
- Read `references/perception-prompt.md` when submitting one proxy video to a perception provider or importing its result.
- Read `references/gemini-worker.md` when operating or debugging the optional Gemini PWA queue adapter.
- Read `references/provider-contract.md` when configuring or implementing a Perception/Review Provider.
- Read `references/qwen-api.md` before enabling Qwen API Perception.
- Read `references/setup-doctor.md` when configuring a new machine or interpreting Doctor codes.
- Read `references/template-v1.md` when changing pacing or extending vertical-video templates.
- Read `references/subtitle-audio-style.md` when matching the benchmark-derived visual and audio preset.
- Use `assets/default-config.json` as the versioned default configuration.
