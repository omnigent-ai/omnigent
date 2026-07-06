# Superpowers Source

This directory vendors the Codex-compatible Superpowers skills from:

- Repository: https://github.com/obra/superpowers
- Upstream commit: d884ae04edebef577e82ff7c4e143debd0bbec99
- Upstream plugin version: 6.1.1

The skills live under `examples/polly/agents/codex/skills/` so the existing
Codex path can find them: `omnigent.spec.skill_sources.resolve_harness_skills`
discovers bundle skills, and `omnigent.runner.app` calls
`omnigent.inner.codex_executor.populate_codex_skills_from_bundle(...)` to link
that bundle into each private per-session `CODEX_HOME/skills/` before Codex
starts.
