# Agent integration

Video OS exposes one stable CLI control surface. Read the repository-root
[AGENTS.md](../AGENTS.md) before operating a project.

Agents should use `video_os.py setup|doctor|status|run|repair|feedback|report`
and the Worker lifecycle commands. They must not invoke lower-level Pipeline
scripts as a substitute for production orchestration, edit state directly, or
fabricate validation and governance artifacts.

The Core is agent-neutral. Codex is a supported operator, not a runtime
dependency. MCP is intentionally outside the v7.5 Public Beta surface.
