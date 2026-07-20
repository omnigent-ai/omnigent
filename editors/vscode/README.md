# Omnigent for VS Code

A minimal VS Code extension that opens your **running local Omnigent server** inside
the editor — an editor-beside pane that iframes the same UI you see at
`http://127.0.0.1:6767`. It is a thin client of the local server's existing HTTP API
(`server/API.md`); there is nothing new to run on the server side.

This is the first, deliberately small donation (tracking
[omnigent-ai/omnigent#1219](https://github.com/omnigent-ai/omnigent/issues/1219)):
localhost discovery, the editor iframe pane, and the activity-bar / editor-title icons.
Sessions, diffs, send-selection, and remote/embedded rendering are intentionally out of
scope for now.

## How it works

- On activation the extension discovers a locally running server via
  `~/.omnigent/local_server.pid` and a `/health` probe (or uses `omnigent.serverUrl`
  when set to a localhost URL).
- The Omnigent activity-bar view offers an **Open Omnigent** button. The
  **Omnigent: Open** command (`omnigent.open`) — also on the editor title bar and in the
  command palette — opens an editor-beside pane that frames the running server.
- The iframe path is used for **local** servers only; a local server is loopback and
  needs no auth, so no token ever appears in the iframe URL.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `omnigent.serverUrl` | `""` | Manual **localhost** server URL override (e.g. `http://127.0.0.1:6767`); empty = auto-discover. Non-localhost URLs are not supported in this build. |

## Known limitation

On macOS, VS Code does not deliver `Cmd+A/C/V` keystrokes into a cross-origin iframe
inside a webview, so keyboard paste into the framed app's inputs does not work there.
This is an upstream VS Code issue, not fixable from the extension for the iframe render
path — see microsoft/vscode#129178 and microsoft/vscode#182642. (The on-page copy
buttons and `navigator.clipboard` paths still work.)

## Build / test / package

```bash
npm ci
npm run type-check   # tsc --noEmit
npm run test         # vitest run
npm run build        # esbuild -> dist/extension.js
npm run package      # @vscode/vsce package -> omnigent-vscode-<version>.vsix
```

Install the resulting `.vsix` via the Extensions view → "Install from VSIX…". The
`.vsix` runtime is `dist/extension.js` + `media/`.

## Layout

```
src/
├── extension.ts        # activate()/deactivate() — wires discovery + panel + command + view
├── commands/openPanel.ts  # the omnigent.open command
├── panel/              # EditorPanelController, host.ts (render), iframeHtml.ts, csp.ts
├── config/             # settings + localhost server-target resolution
└── discovery/          # local-server discovery (pidfile / health / liveness)
```

Licensed under Apache-2.0 (see `LICENSE`). Contributions require a DCO sign-off
(`git commit -s`), per the repository `CONTRIBUTING.md`.
