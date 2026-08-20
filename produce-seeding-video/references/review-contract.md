# Final output review contract

The review layer is a configurable post-render quality gate and is enabled by
default in Video OS. A multimodal provider watches the rendered output and
reports objective content, continuity, and audio defects; the local Director
decides whether to fix and re-render.

## Purpose

- Review is a separate durable queue from perception, stored under `review/`.
- It never replaces local algorithmic QC (`qa_report.json`); it adds content and
  aesthetic judgment that local metrics cannot provide.
- A review result must be validated before it can trigger a re-render.

## Layout

```text
project/
└── review/
    ├── tasks/{queued,running,uploading,uploaded,analyzing,validating,done,failed,needs_login,needs_human}/
    ├── provider_responses/
    ├── results/
    ├── review.json
    └── project_review_manifest.json
```

## Task fields

- `schema_version`: 1.
- `task_type`: `"review"`.
- `task_id`: stable unique id.
- `status`: one of the durable queue states.
- `project_dir`: absolute project path.
- `target`: project-relative rendered output path (normally `output/final.mp4`).
- `target_duration`: rendered duration in seconds.
- `target_signature`: `size_bytes`, `mtime_ns`, `sample_sha256` from
  `source_signature`.
- `proxy_path`: absolute path of the review proxy that keeps the original
  timebase and legible subtitles.
- `script_path`: absolute script path used for alignment context.
- `edit_plan_path`: absolute rendered edit plan path for fix planning context.
- `prompt_contract`: `"references/review-prompt.md"`.
- `result_path`: absolute path under `review/results/`.
- `error`: latest failure reason.

## Result rules

- `verdict` is `pass` or `fix`.
- Every `issue` has `category`, `severity`, `start`, and `end`.
- `start` and `end` stay inside the target duration.
- Categories must come from the fixed enum in `review-prompt.md`.
- `high` severity means must-fix; `medium` means should-fix; `low` means
  optional polish.
- The local pipeline rejects a result whose JSON does not match the schema,
  whose timestamps exceed the target duration, or whose provider response was
  not saved as JSON.

## Usage

The normal production path is `video_os.py run <project-dir> --to FINAL`.
After local QA, the Director prepares a signature-bound task, invokes the
configured Worker for Review only, validates `review.json`, and either advances
or enters REPAIR. The commands below remain useful for diagnostics and manual
queue operation:

1. `prepare_perception.py prepare-review <project-dir> --work-root <large-drive>`
2. `gemini_worker.mjs once --config <worker-config.json> --project <project-dir> --kind review --fail-closed`
3. `prepare_perception.py import-review-result <project-dir> <task-id> <response.json>`
4. Read `review/review.json`; create a fix plan or accept the output.
5. Never mark `done` before a schema-valid result is stored.
6. A Provider timeout, invalid response, unavailable Worker, or mismatched task
   is `needs_human`; no local fallback may synthesize a Review pass.
