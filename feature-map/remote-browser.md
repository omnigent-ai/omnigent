# Remote browser

A workspace tab that shows a Chrome running on the session's host as a live picture. The session's owner can click, type, scroll and navigate in it; other viewers watch. It is off unless the server enables `remote_browser` and the host's runner is given a Chrome DevTools address; see the [operator guide](../docs/remote-browser.md).

## Sub-features

- `remote-browser-open`: with the feature on, **+** > **Remote browser** opens one closable tab per session, remembered on this device.
- `remote-browser-view`: the page appears as a live picture sized to the pane, and follows pane resizes.
- `remote-browser-input`: clicks, drags, wheel scrolling, typing and paste reach the page; the address bar, Back, Forward and Reload navigate it.
- `remote-browser-tabs`: when Chrome has several tabs, a toolbar picker switches the picture between them.
- `remote-browser-view-only`: a viewer who does not own the session sees the picture with a "View only" badge and cannot act on it.
- `remote-browser-unavailable`: a host with no browser configured says so and stops retrying; a dropped stream says it is reconnecting and reattaches on its own.

## How to get to it (user POV)

- Workspace rail: **+** > **Remote browser**. The entry is absent when the server has the feature off.
- Reopen: the tab stays in the rail's tab strip until closed, including after a reload.

## Driving it with the repro environment

Preconditions: a server started with `OMNIGENT_FEATURES=remote_browser`, and a host whose runner has `OMNIGENT_REMOTE_BROWSER_CDP_URL` pointing at a Chrome started with `--remote-debugging-port` and its own `--user-data-dir`.

- Runner bridge (frame pacing, input translation, navigation rules, tab switching, dialogs): `uv run --no-sync pytest tests/runner/test_remote_browser.py`.
- Server relay and access levels: `uv run --no-sync pytest tests/server/routes/test_remote_browser_attach.py`.
- Pane, protocol and rail wiring: `pnpm --filter web test src/components/RemoteBrowserPane src/shell/WorkspacePanel.test.tsx src/shell/workspaceActiveView.test.ts`.
- Human check: open the tab, load a page from the address bar, click a link, type into a field, scroll, resize the rail, and confirm each shows in the picture. Stop Chrome and confirm the pane says it is reconnecting, then start it again and confirm the picture returns.

## Gotchas

- The picture is a canvas, so page text is not in the DOM; assert on what the runner received or on the page itself, not on the pane's text.
- The desktop app's Browser tab and the agent's `browser_*` tools are a different surface and do not drive this Chrome.
- Only the classic workspace rail has the entry; the tabbed workspace sidebar (`workspace_sidebar_v2`) does not yet.
- A Chrome on the host's own display is driven by the pane and by anyone at that machine at once.
