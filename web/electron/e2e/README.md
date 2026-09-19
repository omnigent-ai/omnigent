# Desktop-shell recording lane (Electron)

The reproduction/recording lane for bugs that live in the **Electron desktop
shell** — the main process, not the SPA. Examples: the dead-end 401 fallback
to the setup page, in-window IdP rendering (RFC 8252), the session-expiry
reload, window-open / OAuth-popup policy, the native host-enrollment dialog.

## Why this isn't a `tests/e2e_ui/` pytest lane

Every other recording lane is a pytest-playwright test under `tests/e2e_ui/`,
because the bug is in the SPA and pytest-playwright's `--video` films the
browser page. The desktop shell is different on two counts:

1. **The defect is in Electron's main process**, which a plain browser page
   never exercises — you have to drive the real packaged app.
2. **Python Playwright has no Electron API.** `_electron.launch()` exists only
   in the JavaScript Playwright. So this lane is a small **JS** harness in the
   `web/electron` package, run with `node --test` (the same runner as the rest
   of `web/electron/test/`), not pytest.

The harness still spawns the **same** mock-LLM + `omnigent server` pair the
Python suite spawns (`desktopHarness.js` mirrors the env + argv of
`tests/e2e_ui/conftest.py`), so the shell talks to the same deterministic fake
backend — no real provider creds.

## Files

- `desktopHarness.js` — spawns the mock LLM + `omnigent server`, and launches
  the real desktop shell under `_electron.launch({ recordVideo })` in an
  isolated `userData` dir. `launchDesktop({ serverUrl })` pre-seeds a saved
  server so the app boots straight into the shell (skip connect); omit it to
  film the connect journey.
- `desktop_connect.e2e.js` — the reference test to **copy** for a desktop bug:
  launch → setup page → type URL → Connect → land in the shell. Its `.webm` is
  the desktop journey footage.

## Prerequisites

`electron` and `playwright` are `web/electron` devDependencies (Playwright is
the one with the `_electron` API). The fast `web-test` CI job installs with
`--filter web`, so it never pulls them — only a `web/electron` install does:

```bash
# From the repo root, once:
pnpm --filter web run build          # build the SPA the server serves
cd web/electron && pnpm install      # brings in electron + playwright
```

On a headless CI box, wrap the run in `xvfb-run` so Electron has a display.
The harness still skips cleanly (not fails) when `electron` or `playwright`
are absent (e.g. a `--filter web`-only checkout), so those runs stay green.

## Running

```bash
cd web/electron
# after building the SPA (see above):
node --test e2e/desktop_connect.e2e.js
# headless CI (needs a virtual display; set OMNIGENT_PW_NO_SANDBOX so Electron's
# Chromium starts under xvfb / as root / in a container — same flag the Python
# e2e_ui suite uses):
OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_connect.e2e.js
```

`spawnServer` runs `omnigent server` via `python3` by default; point it at the
right interpreter with `OMNIGENT_PYTHON` when your `omnigent` lives in a venv:

```bash
OMNIGENT_PYTHON=/path/to/.venv/bin/python node --test e2e/desktop_connect.e2e.js
```

The recorded video lands in `e2e/recordings/<slug>/`. Playwright writes one raw
`page@<hash>.webm` per page context — the main shell window, plus any OAuth
popup or in-window IdP view, which record separately. Call
`saveRecording(recordDir, "<name>")` after `electronApp.close()` (as the
reference test does) to rename **all** of them to stable names: the largest
becomes `<name>.webm` and any others `<name>-2.webm`, `<name>-3.webm`, … It
returns the list of saved paths. For a popup / IdP bug the subject is the popup
clip (often the smaller one), so when there is more than one, inspect each and
point the handoff at the one that shows the failure — don't assume the largest.
As with every lane, recordings are workspace artifacts — leave them
uncommitted; CI's artifact bundle collects them.

## Floating Design editor inside modal forms

`desktop_design_prompt.e2e.js` exercises the real SPA, desktop, local server,
runner, and mock model against native HTML and Radix dialogs. It checks direct
form editing, native keyboard focus, floating-editor typing, Escape/close,
normal chat submission, and returning to form editing after Design mode exits.
The Radix fixture is a transformed, scrollable form; the test also checks popup
positioning, clipping, scrolling, resizing, zooming, and accidental form submission.
The same picker and fixtures run in headless Chromium in the regular UI CI suite:
`uv run --no-sync pytest tests/e2e_ui/desktop/test_design_mode_dialog_focus.py`.

```bash
# From the repository root after installing dependencies:
pnpm --filter web run build
pnpm --filter omnigent-desktop-electron run build:overlay
OMNIGENT_PYTHON="$PWD/.venv/bin/python" \
  node --test --test-concurrency=1 web/electron/e2e/desktop_design_prompt.e2e.js
```

Run on an interactive desktop and keep the test app foregrounded for its native
focus assertions. `OMNIGENT_DESKTOP_EXECUTABLE` may point to a packaged binary.
`OMNIGENT_DESKTOP_COMPOSITED_VIDEO=1` records the native window on macOS (requires
Screen Recording permission). Set `OMNIGENT_DESKTOP_RECORD_DIR` outside the
checkout; each fixture's subdirectory receives `design-prompt-native.mov`.
These captures include both the shell and embedded browser; Playwright's renderer
recordings do not capture their native composition.

Human check: select a modal field containing `W41` in Design mode, type `asdf`
into the nearby floating editor, and confirm the field remains `W41`. Escape
should close only the editor. Reopen it and use Send or Enter to submit an
instruction to chat. No instruction bar should appear below the browser URL.

## Authoring a desktop reproduction

1. Copy `desktop_connect.e2e.js` to `e2e/desktop_<slug>.e2e.js`.
2. If the bug's failure is **past** connect, pass `serverUrl` to
   `launchDesktop` so the app boots straight into the shell; if the bug is in
   the connect / setup / fallback flow itself, launch without it (as the
   reference does) and drive the setup page.
3. Drive the real window to the failing state and assert on it. For a
   `reproduced` facet the assertion FAILS on the running build — the failing
   run's video is the before-fix footage.
4. Close the app AND call `saveRecording` in the `finally` block (as the
   reference test does), so the clip is named on the failing path too — a
   `reproduced` facet's run throws, and if you name the clip after the
   `try/finally` it never runs on the very path that produces the before-fix
   footage. Use `saveRecording(RECORD_DIR, "before-<facet>")` (or
   `fixed-<facet>`), then write the journey `caption` for the handoff exactly as
   the other lanes do (see `dev/repro-agent/AGENTS.md`, Step 4).
