# obra/superpowers provenance

- Upstream repository: https://github.com/obra/superpowers
- Tag: `v6.4.2`
- Commit: `8ca22dba9a94f28898bbce59f2537ff4d87c747d`
- Retrieved: 2026-10-02
- Vendored content: upstream `skills/` copied verbatim to
  `examples/superpowers/skills/`

The upstream plugin manifests and session-start hook are intentionally not
vendored. Omnigent parses each `SKILL.md` directly, generates harness manifests
at runtime, and carries the session-start skill-use guidance in this bundle's
agent prompt.
