# OpenCode harness (SDK / `opencode serve` transport) — design

Status: design / pre-implementation
Supersedes: the per-turn `opencode run --format json` approach in
upstream PR #183.

## Goal

Add `harness: opencode` to Omnigent, driving [OpenCode](https://opencode.ai)
through a **persistent `opencode serve` process** talked to over HTTP/SSE via
the **`opencode-ai` Python SDK** (`AsyncOpencode`), instead of spawning
`opencode run` once per turn.

The SDK/serve transport is chosen because it unlocks three capabilities the
per-turn CLI structurally could not provide, all requested for this work:

1. **Mid-turn interrupt** — `client.session.abort(id=…)`.
2. **Live message queue** — a second `client.session.chat(…)` enqueues into the
   running session.
3. **In-process MCP tool-bridge** — spec `tools:` exposed to OpenCode via a
   FastMCP server whose handlers round-trip through Omnigent's dispatch path.

Plus token-usage reporting (free on this transport — the final
`AssistantMessage.tokens` carries the counts).

## Where it plugs in

Same shape as every other wrapped harness:

```
workflow._build_opencode_spawn_env(spec)   →  HARNESS_OPENCODE_* env vars
        │
        ▼  (subprocess spawn)
opencode_harness.create_app()   →  ExecutorAdapter(factory=OpenCodeExecutor)
        │
        ▼
OpenCodeExecutor.run_turn(...)  →  yields TextChunk / ReasoningChunk /
                                    ToolCall* / TurnComplete / ExecutorError
```

`ExecutorAdapter` already provides everything we need: it stamps the
`session_key` onto each message, sets `executor._tool_executor` (our MCP bridge
calls it), forwards interrupt to `interrupt_session()`, forwards steering to
`enqueue_session_message()` (gated by `supports_live_message_queue()`), and
reads per-turn usage off `ctx.provider_usage` (set when we yield
`TurnComplete(usage=…)`).

## Component 1 — server lifecycle (`_OpenCodeServer`)

One `opencode serve` process per harness subprocess (i.e. per Omnigent
conversation), lazily spawned on the **first** `run_turn` that has had a chance
to start the MCP bridge (so the MCP URL is known before the server boots and
reads `OPENCODE_CONFIG_CONTENT`).

- Spawn: `opencode serve --port 0 --hostname 127.0.0.1 --print-logs`
  (cwd = `HARNESS_OPENCODE_CWD`).
- Discover base URL by reading stdout for the line
  `opencode server listening on http://127.0.0.1:<port>` (verified format).
  Timeout + clear error if it never appears.
- Build `AsyncOpencode(base_url=<url>)`. Optionally set
  `OPENCODE_SERVER_PASSWORD` to a random token and pass it as an
  `Authorization` header via the SDK's `default_headers` (loopback-only, so
  defense-in-depth only).
- Health gate: poll `client.app.get()` (or `/` ) until 200 before first prompt.
- Teardown (`close()`): close the SDK client, terminate the process
  (SIGTERM → 5s → SIGKILL), drain stderr-capture task. Mirrors the
  subprocess-lifecycle discipline in `codex_executor.py`.

## Component 2 — turn loop (`run_turn`)

Per turn:

1. Resolve provider/model: `HARNESS_OPENCODE_MODEL` (or `config.model` override)
   is in `provider/model` form → split into `provider_id` + `model_id` for the
   SDK. No model pinned → omit and let OpenCode use its configured default
   (read once from `client.app.get()`/config defaults).
2. Ensure an OpenCode session exists for this `session_key`
   (`client.session.create()`); cache `omnigent session_key → opencode session id`.
3. Extract latest user text (reuse the PR's `_latest_user_text`; multimodal
   deferred as in the PR).
4. **Subscribe to events first**, then send the prompt — avoids the race where
   the model finishes before our SSE subscription is live:
   - `stream = await client.event.list()` (an `AsyncStream[EventListResponse]`).
   - Launch `client.session.chat(id, parts=[{type:"text", text:prompt}],
     provider_id=…, model_id=…, system=system_prompt, tools={…})` as a task.
     `tools` is the `{tool_name: bool}` enable-map; spec tools are surfaced via
     the MCP bridge (Component 3), not this map.
5. Consume events, filtering to our session id, and translate
   (Component 4). End the turn when the awaited `chat()` task returns its
   `AssistantMessage` **and** the stream has drained the matching
   `message.updated`/`session.idle`; whichever is cleaner in practice —
   `chat()` returning the final `AssistantMessage` is authoritative.
6. Yield `TurnComplete(response=<final text or None>, usage=<token map>)`.

Concurrency: the SDK is async; iterate the `AsyncStream` directly in the async
generator (no `iterate_blocking_stream` thread bridge needed). Cancellation of
`run_turn` (HTTP disconnect / adapter cancel) tears down the stream + chat task
and is backstopped by `interrupt_session`.

## Component 3 — in-process MCP tool-bridge (`_OmnigentToolBridge`)

This is the piece the PR explicitly deferred (its follow-up #1).

- On the first `run_turn` carrying non-empty `tools`, build a `FastMCP` server
  (`mcp.server.fastmcp.FastMCP`, already a core dep) exposing one MCP tool per
  `ToolSpec`. Each tool handler calls `self._tool_executor(name, args)` — the
  callback `ExecutorAdapter` installed — which round-trips through
  `ctx.dispatch_tool` (Omnigent policy + execution + the observed/action
  function_call events).
- Serve it over streamable-HTTP on `127.0.0.1:0`; capture the port.
- Inject `HARNESS_OPENCODE_MCP_SERVERS`-equivalent config into the
  `OPENCODE_CONFIG_CONTENT` the server is spawned with:
  `{"mcp": {"omnigent": {"type": "remote", "url": "http://127.0.0.1:<port>/mcp"}}}`.
- Because the OpenCode server reads its config at boot, the MCP server must be
  up **before** `opencode serve` is spawned. So `run_turn` ordering on first
  call is: build bridge (if tools) → synthesise config → spawn `opencode serve`
  → create session → prompt. Turns after the first reuse all of it.
- Tool-set changes across turns: v1 pins the tool-set from the first turn (the
  server is already running). Spec tools are static per agent, so this is
  acceptable; documented as a limitation.
- Shutdown on `close()`.

When `tools` is empty, no bridge is started (matches the PR's warning path, but
now there's a real bridge when tools exist).

## Component 4 — event translation

OpenCode SSE events (`EventListResponse` union, discriminated on `type`):

| OpenCode event | → inner event |
|---|---|
| `message.part.updated` w/ `TextPart` | `TextChunk(text=delta)` |
| `message.part.updated` w/ reasoning text | `ReasoningChunk(...)` (when thinking on) |
| `message.part.updated` w/ `ToolPart` (state `running`/`completed`/`error`) | `ToolCallRequest` + `ToolCallComplete` pair (OpenCode runs the tool itself → observed) |
| `message.updated` (assistant, completed) | capture final text + `tokens` for `TurnComplete.usage` |
| `session.idle` (our session) | end-of-turn signal |
| `session.error` | `ExecutorError` (map `ProviderAuthError`→non-retryable, etc.) |
| others (`file.edited`, `storage.write`, lsp, …) | dropped (debug log) |

Part deltas: OpenCode sends the **full** part on each `message.part.updated`, so
text streaming needs to diff against the last-seen text for that part id and
emit only the suffix. Tracked in a per-turn `{part_id: last_len}` map.

`handles_tools_internally()` stays `True` — OpenCode executes its own built-in
tools; we surface them as observed pairs, and the MCP-bridge tools are executed
by Omnigent but still flow back to OpenCode as MCP results (not re-executed by
the Session layer).

Token usage: `AssistantMessage.tokens` → `{"input_tokens": input,
"output_tokens": output, "cache_read_input_tokens": cache.read,
"cache_creation_input_tokens": cache.write, "total_tokens": input+output}`.

## Component 5 — interrupt + live queue

- `interrupt_session(session_key)`: look up the opencode session id; call
  `await client.session.abort(id=…)`; return `True`. Idempotent / best-effort.
- `enqueue_session_message(session_key, content)`: fire
  `client.session.chat(id=…, parts=[{type:text,text:content}], …)` without
  awaiting completion (OpenCode queues it onto the running session); return
  `True`. `supports_live_message_queue()` → `True`.

## Component 6 — config synthesis (gateway + MCP)

Keep the PR's `_build_opencode_config_content()` design (gateway provider
override via `OPENCODE_CONFIG_CONTENT.provider.<id>.options.{baseURL,apiKey}` +
`OPENCODE_DISABLE_PROJECT_CONFIG=1`), extended so the `mcp` map is produced
in-process from the bridge's live port rather than read from
`HARNESS_OPENCODE_MCP_SERVERS`. The env var is still honoured (operator-supplied
extra MCP servers / disables get merged).

## Surfaces touched (full parity with PR #183, SDK transport)

Runtime: `omnigent/inner/opencode_executor.py` (new, SDK-based),
`omnigent/inner/opencode_harness.py` (new), `runtime/harnesses/__init__.py`
(registry), `runtime/workflow.py` (`AgentHarnessType`, spawn-env builder,
provider/databricks routing), `runner/app.py` (model-override env map +
dispatch), `model_catalog.py`, `model_override.py`, `spec/_omnigent_compat.py`
(allowlist).

Onboarding: `onboarding/harness_install.py` (binary `opencode`, npm
`opencode-ai`, `auth login/logout`), `onboarding/harness_readiness.py`
(`OPENCODE_SURFACE`).

CLI / UX: `cli.py` (`omnigent opencode` subcommand + harness choices + default
prompt + first-run plan), `ap-web/.../AgentCard.tsx` (fallback glyph note),
`README.md`, `docs/AGENT_YAML_SPEC.md`, `examples/opencode_hello.yaml`.

Packaging: `pyproject.toml` — `opencode-ai` (pre-release) is an **optional**
dependency, exposed as the `opencode` extra (`pip install "omnigent[opencode]"`)
and folded into `all`. It is imported lazily inside `_OpenCodeServer.start`, so
a default install never pulls it; the executor raises an actionable `ImportError`
when the harness is used without it.

Tests: harness unit tests, spawn-env/provider-routing tests, onboarding
install/readiness tests, CLI test, and an opt-in e2e gated on
`OMNIGENT_E2E_OPENCODE=1` + `opencode` on PATH (server boot → prompt → text →
usage; plus MCP-bridge variant).

## Key decisions / defaults (call out for review)

- **One server per conversation subprocess** (not per Omnigent session key).
  Each harness subprocess already maps to one conversation, matching every
  other wrap. Multiple Omnigent "sessions" inside one subprocess share the
  server but get distinct OpenCode session ids.
- **`--port 0`** then parse the announced port (observed: opencode still
  defaulted to 4096, but parsing the stdout line is robust to either).
- **Skip-permissions defaults to True** (headless) — same as the PR.
- **Tool-set pinned at first turn** for the MCP bridge — acceptable since spec
  tools are static per agent.
- **Auth token materialisation** for gateway providers without a static key:
  keep the PR's one-shot `_materialise_one_shot_token` (non-refreshing,
  documented).

## Status: implemented

Landed across 13 TDD tasks. Module layout:

- `omnigent/inner/opencode_executor.py` — helpers, config synthesis, event
  translation, `_OpenCodeServer` lifecycle, `_OmnigentToolBridge` (in-process
  FastMCP), and `OpenCodeExecutor` (turn loop + interrupt + live queue).
- `omnigent/inner/opencode_harness.py` — `create_app()` wrap.
- Registry / spec allowlist / `AgentHarnessType` / spawn-env + provider &
  Databricks routing (`workflow.py`) / runner dispatch (`runner/app.py`) /
  model catalog + override / onboarding install + readiness / `omnigent
  opencode` CLI / docs / `examples/opencode_hello.yaml` / frontend glyph note.

Tests: 40 opencode unit tests + 1 opt-in e2e (`OMNIGENT_E2E_OPENCODE=1`).
`ruff` clean across all touched files; `mypy` clears every error except the
`explicit-any` class, which the sibling inner executors (e.g.
`codex_executor.py`) also carry at the untyped-SDK boundary.

### Notes / details settled during implementation

- **`session.chat` requires both `provider_id` and `model_id`** (no SDK
  defaults). When the model is unpinned or carries no provider prefix, both
  are resolved from `client.app.providers().default` (a `{provider_id:
  model_id}` map) and cached. Applies to `enqueue_session_message` too.
- **No reasoning Part type** in `opencode-ai` 0.1.0a36 — the Part union is
  text/file/tool/step*/snapshot/patch — so reasoning does not stream even with
  `HARNESS_OPENCODE_THINKING=1`. The translator's reasoning branch is retained
  for forward-compat but never fires.
- **MCP tool schema registration**: `FastMCP.add_tool` infers the schema from
  the handler signature and has no JSON-schema parameter, so each spec tool is
  registered by synthesising a keyword-only `__signature__` from its
  `parameters`, building the `Tool` via `Tool.from_function`, then overriding
  `tool.parameters` with the original spec schema so OpenCode sees real types.
- **Chat task lifecycle**: the `session.chat` task is reaped (cancelled +
  awaited) on every non-idle exit (`session.error`, exception, cancellation)
  so it can never outlive the turn.
- The tool-bridge uvicorn server runs with `ws="none"` (the MCP transport is
  POST/SSE only) and raises if it does not reach `started` within ~5s.

### Deferred (documented; match the PR's follow-up list)

- **Multimodal input** — image/file/audio blocks are dropped with a warning in
  `_latest_user_text`; only text reaches OpenCode.
- **Native TUI** (`opencode-native`, tmux-pane parity with `omnigent claude` /
  `omnigent codex`).
- **Dedicated frontend glyph** — `AgentCard.tsx` falls through to `BotIcon`.
- **`--continue` / last-session resume** affordance.
- **Three extra provider-default integration tests** from the PR reference
  (anthropic global default, openai-family fallback, workdir→cwd) — the unit
  builder tests cover the core paths; these can be ported into
  `tests/runtime/test_provider_spawn_env.py` later.
- **uv.lock regeneration** for the new `opencode-ai` dependency before release.
