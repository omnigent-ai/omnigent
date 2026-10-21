---
name: otto-drive-pr
description: PR mergeability, CI, and live preview.
---

# Shared PR-driving resources

Read each resource when the agent's PR-driving procedure reaches its substep:

| Substep | Resource |
| --- | --- |
| 4.2, current CI and mergeability | [ci.md](ci.md) |
| 4.1, preview after CI and both reviews are clean | [preview.md](preview.md) |

Use `read_skill_file` with this skill name and the relative filename, or read
relative to the directory supplied by the native Skill tool.

Substep numbers follow Resolve's `resolve-drive-pr`: 4.3 is the shared
`otto-pr-review-loop`, and 4.4/4.5 are the agent's own live-validation and
maintainer-handoff steps. "Fix" and "bug" mean the PR's change. The review path
and fork takeovers apply only to agents that review other authors' PRs. Handoff
fields named here (`ci_status`, `ui_preview`, `validation_surface`,
`files_changed`) are Resolve's; record the same facts in your agent's handoff.
