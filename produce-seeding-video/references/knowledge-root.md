# Video OS Knowledge Root contract

Memory/Knowledge commands use one explicit runtime root. Configure it with a
command option (`--knowledge-root` in `video_os.py`, `--root` in
`knowledge_tools.py`) or the `VIDEO_OS_KNOWLEDGE_ROOT` environment variable.
The value must be an absolute path. Runtime code does not infer a Knowledge
Root from the script location or current working directory.

Use `python scripts/knowledge_tools.py status` to distinguish these states:

- `unconfigured`: neither a CLI path nor the environment variable is set.
- `path_missing`: a configured path does not exist.
- `initialized_empty`: `manifest.json` is valid and no Knowledge data exists.
- `ready`: the initialized root contains Knowledge data.

An existing directory without a manifest is `uninitialized`; malformed roots
are `invalid`. Consumers fail closed for every state except
`initialized_empty` and `ready`. Only `knowledge_tools.py init` may create a
root, and it also requires an explicit CLI or environment configuration.

Knowledge data is runtime state and is not part of the installed Skill release
inventory. Installation must copy code and this contract, never credentials,
private feedback, reviews, browser sessions, or runtime Knowledge records.

Evidence extraction accepts only `evidence_tier: production_verified` by
default. `demo`, `migrated_unverified`, and records without a recognized tier
remain available for audit but cannot create candidates.

Reviewed candidates are immutable. If materially new verified evidence supports
the same logical rule family, extraction creates a new candidate revision with
`lineage_id`, increasing `revision`, and `supersedes_candidate_id`; it never
rewrites the reviewed candidate or its review/rule evidence snapshot.

Approval creates an inactive Rule. Planner use requires a second explicit human
`activate` decision bound to the exact Rule ID, revision, content hash, approval
review, reviewer, reason, timestamp, and `advisory` application mode. Activation
never carries to a new revision. Deprecated, revoked, inactive, invalid, or
unsealed records cannot enter Planner Memory. Knowledge failure is visible in
the PLAN artifacts and falls back to the Base Plan; it does not fabricate use or
block an otherwise valid production plan.
