# Files and comments

## Sub-features

- Workspace file list, search, preview, edit, autosave, and diff
- Markdown, HTML, PDF, notebook, code, and image rendering
- Inline comments, actions, realtime updates, and inbox
- Runner-unavailable and read-only degradation

## How to get to it

Open a chat with a filesystem-capable agent, expand the Workspace rail, select a
file, and preview or edit it. Add comments from supported viewers or resolve
them from the inbox.

## Driving it with Playwright

Use focused tests under `tests/e2e_ui/files` and `comments`;
`scripts/verify.sh core` includes autosave. Open the rail by its `Workspace`
accessible name and use existing viewer or test IDs. After editing, read the
file through the resource API or reload the viewer. After commenting, read the
comment endpoint and check realtime visibility.

The observable end state is both visible UI and persisted runner/server state.

## Gotchas

- Direct runner resource paths require an `os_env`.
- The default E2E runner lacks workspace affinity, so git diff is not available
  without a dedicated fixture.
- Runner 503s should degrade the UI rather than crash it.
- Cleanup removes scratch files but must retain screenshots, traces, and logs.
