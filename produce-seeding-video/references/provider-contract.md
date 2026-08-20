# Provider contract

The Provider boundary executes one existing durable `perception` or `review`
task. It does not create queue tasks, mutate Director state, merge Perception,
validate Review, plan edits, render, or decide whether a project reaches FINAL.

Provider selection precedence is project stage configuration, stage-specific
environment configuration, shared user Provider configuration, then the legacy
Gemini Worker default. `none`, unknown, unavailable, timed-out, non-zero, idle,
malformed, or task-mismatched results fail closed.

Gemini Browser Worker and Qwen API implement the Perception boundary. Qwen API
is Perception-only; automatic Review retains its separately configured Provider.
Existing worker/profile/config behavior is unchanged. A Provider success is only an
invocation result; the owning manager must still verify the exact durable task
state and current signature-bound artifact before the Director can advance.
