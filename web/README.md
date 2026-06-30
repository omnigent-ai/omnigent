# web

The web UI for `omnigent server --agent <agent>`. SPA built with Vite + React + TypeScript +
Tailwind v4 + shadcn/ui. Talks to the current Omnigent API surface
(`/v1/agents`, `/v1/sessions`, session-scoped
`/v1/sessions/{id}/resources/files`).

## Develop

In one terminal, start the omnigent server (default port `6767`). Use
`--agent` to pre-register one or more agents at startup (accepts a YAML file or
an agent-image directory; can be repeated):

```bash
.venv/bin/omnigent server --agent examples/hello_world.yaml
```

In another terminal, start the Vite dev server (port `5173`):

```bash
cd web
npm install
npm run dev
```

The Vite dev server proxies `/v1` and `/api` to `http://localhost:6767`. Set
`OMNIGENT_URL` to override the proxy target:

```bash
OMNIGENT_URL=http://localhost:9000 npm run dev
```

Additional `omnigent server` options:

| Flag                  | Default                | Description                          |
| --------------------- | ---------------------- | ------------------------------------ |
| `--host`              | `127.0.0.1`            | Host to bind to                      |
| `-p` / `--port`       | `6767`                 | Port to listen on                    |
| `--database-uri`      | `<data-dir>/chat.db`   | Database URI for stores              |
| `--artifact-location` | `<data-dir>/artifacts` | Path for artifact storage            |
| `-c` / `--config`     | (none)                 | Path to YAML config file             |
| `--execution-timeout` | `7200`                 | Max wall-clock seconds per execution |
| `--agent`             | (none)                 | Pre-register an agent (repeatable)   |

## Build + serve from the Omnigent server

```bash
cd web
npm run build
```

Vite writes the bundle to `../omnigent/server/static/web-ui/` (configured in
`vite.config.ts`). When that directory exists and contains `index.html`, the
FastAPI app in `omnigent/server/app.py` mounts it at `/`. After a build:

```bash
.venv/bin/omnigent server --agent examples/hello_world.yaml
# open http://localhost:6767/
```

## Lint + format

```bash
npm run lint          # oxlint .
npm run lint:fix      # oxlint --fix .
npm run format        # prettier --write .
npm run format:check  # prettier --check .
npm run type-check    # tsc -b
```

`npm run type-check` runs in CI as part of the `Pre-commit checks`
job (`.github/workflows/lint.yml`) and gates merge. Run it locally
before committing any change under `web/`.

## Test

```bash
npm run test          # vitest run
npm run test:watch    # vitest in watch mode
```

## Reducer parity

The TypeScript reducer at `src/lib/blockStream.ts` is a hand-mirror of
the Python reducer at
`sdks/python-client/omnigent_client/_stream.py`. Same for:

| TS file                       | Mirrors                                       |
| ----------------------------- | --------------------------------------------- |
| `src/lib/blocks.ts`           | `omnigent_client/_blocks.py`                  |
| `src/lib/events.ts`           | `omnigent_client/_events.py`                  |
| `src/lib/types.ts`            | minimal subset of `omnigent_client/_types.py` |
| `src/lib/sse.ts`              | `omnigent_client/_sse.py`                     |
| `src/lib/blockStream.ts`      | `omnigent_client/_stream.py`                  |
| `src/lib/blockStream.test.ts` | `tests/frontends/sdk/test_stream.py`          |

There is **no cross-language CI gate** today. When `_stream.py`
changes for a real bug (e.g. new harness quirk, dedup edge case), the
TypeScript port can lag — drift surfaces only when someone next runs
`npm run test` after a behavioral change. Workflow when `_stream.py`
changes:

1. Read the diff to `_stream.py` (or `_blocks.py` / `_events.py`).
2. Update `blockStream.ts` (or `blocks.ts` / `events.ts`) to match.
3. Add or update a case in `blockStream.test.ts` that pins the new
   behavior — same shape as `test_stream.py`.
4. `npm run test` → green.

If we ever decide cross-language fixture parity is worth the
maintenance burden, we'd port the captured-fixture approach used
for `test_stream.py`.

### web-only divergences

web carries a few constructs the Python SDK doesn't, on purpose.
They're listed here so a future maintainer doesn't try to "restore
parity" by mirroring them across.

- `UserMessageBlock` (in `blocks.ts`) — surfaces persisted user
  message items as blocks so the bubble walker sees a single flat
  list. The SDK's `BlockStream.stream()` never emits user messages
  (its consumers receive the user input as the caller's own argument,
  not back through the stream).
- `BlockContext.responseId` + `BlockContext.itemId` — populated by the
  TS reducer from the SSE wire format (`response.created.response.id`
  and `event.item.id` / `event.item.response_id` on
  `output_item.done`) so each block knows its server origin. The TS
  events `ToolCall` / `ToolResult` / `MessageDone` / `NativeToolCall`
  carry `itemId` + `responseId` to thread the values through.
- Flat block storage in `chatStore.blocks`, grouped at render time by
  `buildBubbles` keyed on `ctx.responseId`. The SDK has no equivalent
  — its consumers iterate the block stream procedurally without a
  stateful store.

When `_stream.py` / `_events.py` / `_blocks.py` change for a
substantive reason (new event type, new dedup edge case), continue to
mirror the _behavioral_ changes here; just leave the divergences above
alone.

## Stack

- Vite + React 19 + TypeScript
- Tailwind v4 (`@import "tailwindcss"`, no config file)
- shadcn/ui (`radix-nova` preset, neutral base, CSS variables)
- TanStack Query, Zustand, React Router v7
- streamdown (+ `@streamdown/code`, `@streamdown/math`, `@streamdown/mermaid`),
  shiki, framer-motion, cmdk, react-hotkeys-hook, use-stick-to-bottom,
  next-themes, react-hook-form, zod
- Lint: oxlint. Format: prettier.
