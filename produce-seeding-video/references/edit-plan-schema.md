# Edit plan schema

`edit_plan.json` is the stable boundary between creative planning and deterministic rendering.

## Planner Memory layers

PLAN always preserves four auditable layers:

- `edit_plan.base.json`: independently reproducible output of the deterministic Base Planner.
- `memory_context.json`: current project, input, Perception, Base Plan, activated Rule revision, content hash, activation, and Knowledge source bindings.
- `memory_application.json`: one explicit decision per matched Rule plus any narrowly scoped structured diff.
- `edit_plan.json`: executable Final Plan and signed Memory provenance.

`video_os.planner_memory.mode` accepts only `off`, `shadow`, or `advisory` and defaults to `shadow`. Shadow may record `would_apply` decisions but its executable Final Plan must remain semantically equal to the Base Plan. Advisory rules are soft constraints: unsupported, conflicting, stale, invalid, or unsafe advice is recorded and falls back to the Base Plan. No Rule can enter this layer without a current human activation for its exact revision and content hash.

## Top-level fields

- `schema_version`: Integer schema version. Styled-subtitle/audio plans use `2`.
- `template`: Template identifier.
- `canvas`: `width`, `height`, and `fps`.
- `duration_seconds`: Expected total duration.
- `segments`: Ordered executable clip segments.
- `subtitles`: Subtitle enablement, format, filenames, preset, timing mode, and hook/CTA boundaries.
- `bgm`: Optional project-relative path, volume, fades, and speech-ducking parameters.
- `sound_effects`: Ordered skill-relative assets with semantic segment and absolute timeline position.
- `audio`: Voice gain and final loudness-normalization targets.
- `render`: Encoder and output settings.
- `warnings`: Recoverable planning warnings.
- `memory`: Base Plan, Context, Application signatures; mode; actual applied/skipped Rule revisions; warnings/fallback; and explicit `memory_applied` state.

## Segment fields

- `id`: Stable segment role.
- `intent`: Human-readable creative intent.
- `timeline_start` / `timeline_end`: Output timeline boundaries in seconds.
- `duration`: Segment duration in seconds.
- `source`: Project-relative media path.
- `source_start`: Input trim position in seconds.
- `source_duration`: Probed source duration.
- `has_audio`: Whether the selected source has audio.
- `loop`: Whether FFmpeg must loop the source to fill the segment.
- `matched_tags`: Role tags that influenced selection.
- `template_segment`: The parent template role when one role is split into several short shots.
- `selection.mode`: `perception`, `metadata-fallback`, or legacy selection provenance.
- `selection.perception_segment_id`: Source segment identifier from validated `perception.json`.
- `selection.safe_start` / `safe_end`: Source-local safe trim boundaries.
- `selection.quality_score` / `confidence`: Observational quality values supplied by the perception layer.
- `selection.visual_fingerprint`: Stable visual identity used to reject repeated shots.
- `selection.duplicate_reuse`: Must remain `false` unless a future explicit creative rule authorizes repetition.

## Invariants

- Sort segments by `timeline_start`.
- Start the first segment at `0`.
- Keep adjacent boundaries continuous.
- Keep every duration positive.
- Make final `timeline_end` equal `duration_seconds`.
- Resolve every `source` under the project directory.
- Never reference a `reference/` file without explicit user authorization.
- Record all short-source loops and fallback selections.
- Keep every perception-guided trim fully inside its validated safe range.
- Reject repeated `visual_fingerprint` values by default.
- Treat `metadata-fallback` as requiring human review when validated perception exists elsewhere in the plan.
- Resolve `bgm.path` under the project directory.
- Resolve every `sound_effects.events[].asset` under the Skill directory.
- Keep every sound-effect `at` value within the output duration.
- Keep the ASS primary file and SRT compatibility file on the same cue timeline.

The renderer rejects discontinuous timelines, missing sources, invalid canvas sizes, and non-positive durations.

## Jianying editable-project export

The optional Jianying export is a deterministic consumer of the same plan. It must preserve source trim positions and timeline positions, and split editable content into named tracks for primary video, B-roll, sound effects, normal subtitles, flower text, and BGM. Keep the FFmpeg preview as the visual reference; the Jianying draft is the manual-refinement deliverable.
