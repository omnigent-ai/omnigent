# Omnigent Canvas

An independently installable browser extension that visualizes accessible Omnigent sessions as draggable cards on a React Flow canvas.

Each card shows the session status, title, and working directory. Double-click a card (or focus it and press Enter/Space) to open Omnigent's existing session transcript.

![Canvas showing three sessions](../../docs/demo/canvas.png)

## Install from this checkout

Build the browser bundle, install the Python distribution, and start Omnigent:

```bash
pnpm --filter @omnigent/canvas build
uv pip install -e ./extensions/canvas
uv run omnigent
```

The **Canvas** item then appears in primary navigation. Omnigent discovers the package through its `omnigent.extensions` entry point; no core source changes are required to register it.

## Development

```bash
pnpm --filter @omnigent/canvas type-check
pnpm --filter @omnigent/canvas test
pnpm --filter @omnigent/canvas build
```

The committed build artifacts live in `src/omnigent_canvas/dist/` so the Python package is immediately usable after installation.

## Storage and privacy

The extension requests only `navigation`, `sessions.read`, and `storage.user`. The host filters the session list to the current user's accessible, top-level, non-archived sessions. While Canvas is open, the parent session stream sends ID-free invalidation events so visible cards refresh immediately. Hidden tabs defer and coalesce updates until they become visible. A low-rate 30-second reconciliation covers sessions outside the parent stream's bounded watch set. Every refresh performs a new permission-filtered list operation.

Manually arranged card positions and the viewport are stored locally in extension-scoped browser storage; transcript content is never read by this extension.
