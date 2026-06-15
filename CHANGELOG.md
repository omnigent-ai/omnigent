# Changelog

All notable changes to this project should be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it's out of alpha. While the project is in alpha (current state per
the README badge and `pyproject.toml` Development Status), breaking
changes can land on `main` and they're called out here under
`### Changed (breaking)`.

## [Unreleased]

### Added
- `SessionsChat(hooks=...)` and `OmnigentClient.sessions_chat(hooks=...)`
  now accept a `StreamHooks` instance and fire response, reasoning,
  message, tool-call, file-output, and elicitation callbacks from the
  sessions-first event stream. See PR #43.

### Fixed
- Circular import between `omnigent.llms` and `omnigent.reasoning_effort`
  that blocked server startup on a fresh install. See PR #149.

### Notes for maintainers filling this in retroactively

This file is a scaffold proposed in PR #<this-pr>. Please backfill
prior released versions (or the most recent ~10 PRs if there aren't
any tags yet) in the format below. Future merged PRs should add a
one-line bullet under the appropriate `### Added / Changed /
Deprecated / Removed / Fixed / Security` heading under
`## [Unreleased]`, and the release workflow
(`.github/workflows/release-omnigent.yml`) should promote the
`## [Unreleased]` section to a versioned heading at tag time. That
way a release tag automatically captures what's changed since the
last one.

Example shape once tags start landing:

```markdown
## [0.2.0] - 2026-07-15

### Added
- ...

### Changed (breaking)
- ...

### Fixed
- ...

[0.2.0]: https://github.com/omnigent-ai/omnigent/compare/v0.1.0...v0.2.0
[Unreleased]: https://github.com/omnigent-ai/omnigent/compare/v0.2.0...HEAD
```
