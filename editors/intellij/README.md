# Omnigent for IntelliJ / PyCharm

A minimal IntelliJ Platform plugin that opens your **running local Omnigent server**
inside the IDE — a tool window that live-navigates a JCEF (Chromium) browser to the
same UI you see at `http://127.0.0.1:6767`. It is a thin client of the local server's
existing HTTP API (`server/API.md`); there is nothing new to run on the server side.

This is the IntelliJ/PyCharm counterpart to the VS Code minimal slice
(`editors/vscode/`): localhost discovery, a tool-window pane, and an "Open" action.
Sessions, diffs, send-selection, and remote/embedded rendering are intentionally out
of scope for now.

## How it works

- On open, the tool window discovers a locally running server via
  `~/.omnigent/local_server.pid` and a `/health` probe (or uses the `Server URL`
  setting when set to a localhost URL).
- The **Omnigent** tool window (right-hand side) live-navigates a `JBCefBrowser` to
  the resolved server once it is healthy. **Omnigent: Open** (Find Action / Tools
  menu) focuses it.
- JCEF is same-origin full Chromium (no iframe, no CSP bundling needed), so the render
  path is used for **local** servers only — a local server is loopback and needs no
  auth, and the loopback gate is re-asserted at the render surface before navigating.

## Settings

Settings → Tools → Omnigent:

| Setting | Default | Purpose |
|---|---|---|
| Server URL | `""` | Manual **localhost** server URL override (e.g. `http://127.0.0.1:6767`); empty = auto-discover. Non-localhost URLs are not supported in this build. |

A `Server URL` change is picked up on the next tool-window open (close + reopen, or
IDE restart), not a live re-render.

## Known limitations

- Requires a JCEF-capable JetBrains Runtime; if the current IDE runtime lacks JCEF the
  tool window shows guidance to switch it (Find Action → "Choose Boot Java Runtime for
  the IDE").
- Unlike the VS Code extension, JCEF is same-origin full Chromium, so the macOS
  cross-origin-iframe paste limitation does not apply here — copy/paste into the framed
  app is expected to work normally (verified manually; see `CONTRIBUTING.md`-style
  clipboard smoke in the plan's verification steps).
- The tool-window stripe icon is the VS Code activity-bar SVG as-is; it may render
  oversized/off-tone until it is resized to a 13×13 monochrome icon (cosmetic,
  non-blocking follow-up).

## Build / test / package

```bash
cd editors/intellij
./gradlew test          # JUnit5 unit tests (pure discovery/config/tool-window logic)
./gradlew buildPlugin   # -> build/distributions/omnigent-intellij-<version>.zip
./gradlew runIde        # launch a sandbox IDE with the plugin installed
```

Install the resulting `.zip` via Settings → Plugins → gear icon → "Install Plugin from
Disk…". The first build needs network access to download the IntelliJ Platform
artifact.

## Layout

```
src/main/kotlin/ai/omnigent/intellij/
├── discovery/    # local-server discovery (pidfile / health / liveness)
├── config/       # settings + localhost server-target resolution
├── toolwindow/   # tool-window content decision + the JCEF adapter
└── actions/      # the "Omnigent: Open" action
```

Licensed under Apache-2.0 (see `LICENSE`). Contributions require a DCO sign-off
(`git commit -s`), per the repository `CONTRIBUTING.md`.
