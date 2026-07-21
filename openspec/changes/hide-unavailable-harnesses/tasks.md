## 1. Tasks

- [x] 1.1 Add a dismissed_harnesses config reader/writer that validates harness ids against the harness catalog.
- [x] 1.2 Filter GET /v1/harnesses server-side using dismissed_harnesses while preserving current behavior when the key is absent.
- [x] 1.3 Add CLI commands to list, hide, and unhide harnesses for headless configuration.
- [x] 1.4 Add unit tests for default catalog behavior, filtered catalog behavior, CLI hide/unhide, and invalid harness handling.
- [x] 1.5 Update user-facing docs/help text for the new harness visibility controls.
