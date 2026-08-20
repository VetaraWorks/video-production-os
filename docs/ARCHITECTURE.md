# Architecture

```text
Agent / CLI
    ↓
Video OS Director + validated state transitions
    ↓
ANALYZE → PERCEPTION → PLAN → RENDER → QA → REVIEW
                                      ↑          ↓ fix
                                      └── REPAIR ┘
                                                 ↓ pass
                                               FINAL
```

Perception Providers only obtain analysis for an existing durable task. Their
output must pass prepare/merge/validation and current-input signatures before
Planner can consume it. Planner preserves Base Plan, Memory Context,
explainable advisory application, and Final Plan. Render/QA independently prove
media truth. Review is bound to the complete current final-video signature.

Production Evidence is created only after the production chain proves its
inputs. Candidate extraction, human review, Editing Rule creation, human
activation, and Planner Memory remain separately auditable. No single Review
pass automatically changes a rule.

User config, projects, Knowledge, Worker profile, cache, and logs are outside
the installed Skill. Runtime discovery has explicit-config-first precedence and
supports Python, Node, FFmpeg, ffprobe, Chrome, and Edge without developer paths.
