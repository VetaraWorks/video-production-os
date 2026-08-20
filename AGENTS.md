# Video OS agent operating contract

Video OS Core is agent-neutral. Codex, Claude Code, Gemini CLI, Hermes, and
other coding agents are adapters around the same public CLI; none is a Core
runtime dependency.

## Supported control surface

Run project operations through `produce-seeding-video/scripts/video_os.py`:

- `setup` and `doctor` configure or diagnose a user installation.
- `status <project>` reads Director state.
- `run <project> --to PLAN|FINAL` runs the validated Director chain.
- `repair <project>` plans repair; `--apply` still requires human confirmation.
- `feedback <project>` records validated human feedback.
- `report <project>` creates a redacted diagnostic archive.
- `worker login|status|start|stop` manages the isolated Gemini Worker.

Do not treat `run_pipeline.py`, direct module imports, or hand-edited state as
an equivalent production entrypoint.

## Boundaries agents must preserve

- Do not edit `project_state.json` to move stages or mark completion.
- Do not fabricate `perception.json`, `qa_report.json`, `review.json`, a Review
  pass, repair completion, Production Evidence, human decisions, rule reviews,
  Editing Rules, activation records, signatures, or media validation.
- Do not bypass `ANALYZE -> PERCEPTION -> PLAN -> RENDER -> QA -> REVIEW` or the
  `REPAIR -> RENDER -> QA -> REVIEW` loop.
- Do not write into `raw_video/`, `material/`, or `reference/` inputs.
- Do not place API keys, tokens, cookies, browser profiles, `.env` files, or
  local credentials in Git, logs, reports, or issue attachments.
- Do not bypass Planner Memory activation/provenance gates or make advisory
  memory a hard constraint.

Use the existing CLI contracts and tests. Changes to the Director, state
machine, Pipeline, Planner, Render, QA, Review, Repair, Knowledge, or Memory
require explicit scope and regression evidence.
