# OpenCode harness — known follow-ups

The OpenCode harness landed in #45 with the runtime, CLI, onboarding,
frontend, and gateway-routing wiring complete. This file tracks the
gaps deliberately deferred so they don't get lost. Each item lists
what the gap is, why it was deferred, and a concrete starting point
for whoever picks it up.

## 1. In-process MCP server bridging spec tools into OpenCode

**Gap.** Spec-declared tools (`tools:` block in the agent YAML) are
not exposed to the OpenCode subprocess. `OpenCodeExecutor.run_turn`
accepts the `tools` argument for ABI parity and logs a one-time
warning per session when tools are declared without an MCP bridge
wired (`omnigent/inner/opencode_executor.py` ~line 510).

**Why deferred.** Implementing an in-process FastMCP server inside
the harness subprocess — bound to a UNIX socket or ephemeral port,
routing tool calls back through the `_tool_executor` callback
registered by `ExecutorAdapter`, lifecycle-managed alongside the
turn — is ~300–500 lines of plumbing-heavy code. The Codex
equivalent (`dynamicTools` over the App Server JSONL protocol) lives
in `omnigent/inner/codex_executor.py` and is ~2300 lines total.
Lands cleanest as its own focused PR with the same test gating.

**Starting point.**

1. `mcp.server.fastmcp.FastMCP` is already an installed dep (see
   `.venv/lib/python3.12/site-packages/mcp/`). Build a server class
   that takes the list of `ToolSpec` dicts + a callback and exposes
   each tool via `mcp.add_tool(...)`.
2. Boot it lazily on the first `run_turn` that carries non-empty
   `tools` — bind to `127.0.0.1:0`, capture the port.
3. Set `HARNESS_OPENCODE_MCP_SERVERS` in the spawn env to
   `{"omnigent": {"type": "remote", "url": f"http://127.0.0.1:{port}/mcp"}}`.
   The synthesis path in `_build_opencode_config_content`
   (`omnigent/inner/opencode_executor.py`) already merges that into
   `OPENCODE_CONFIG_CONTENT.mcp` correctly.
4. Shutdown on `close_session`.

Test gating: same pattern as
`tests/e2e/test_opencode_executor_e2e.py` —
`OMNIGENT_E2E_OPENCODE_MCP=1` + `opencode` on PATH.

## 2. Native TUI launch (tmux-pane parity with `omnigent claude`)

**Gap.** `omnigent opencode` is a discoverability shortcut for
`omnigent run --harness opencode` — it runs OpenCode headlessly
behind the standard Omnigent REPL, not the OpenCode TUI in a tmux
pane. There is no `opencode-native` harness analogous to
`claude-native` / `codex-native`.

**Why deferred.** Native TUI integration is a separate piece of
work (~300–500 lines): a `tmux`-based pane manager, a
`OpenCodeNativeExecutor` that wraps the subprocess and bridges
input/output through Omnigent's terminal layer, and the equivalent
of `omnigent/claude_native_*.py` / `omnigent/codex_native_*.py`.
OpenCode's CLI already supports a `--attach` mode aimed at exactly
this use case (see `packages/opencode/src/cli/cmd/attach.ts`), so
the implementation should be easier than Claude/Codex's were.

**Starting point.** Model on `omnigent/codex_native_executor.py` +
`omnigent/codex_native_harness.py`. Adding `opencode-native` to
`OMNIGENT_HARNESSES`, `_HARNESS_MODULES`, and the
`omnigent.codex_native_*` analogue file set is the bulk of the
work. The `--attach` flag means most of the TUI-rendering plumbing
already lives in OpenCode itself.

## 3. Dedicated OpenCode glyph

**Gap.** `ap-web/src/components/AgentCard.tsx` falls through to
`BotIcon` for opencode agents. The other CLI harnesses each have
their own SVG glyph under `ap-web/src/components/icons/`.

**Why deferred.** No canonical SVG to copy from OpenCode's repo
yet. Trivially small once an asset lands — add an
`OpenCodeIcon.tsx`, import it in `AgentCard.tsx`, and switch the
fallback comment to a real branch.

## 4. Multimodal input

**Gap.** Image / file blocks (`input_image`, `input_file`,
`input_audio`) on a user message are dropped with a warning per
`_latest_user_text` in `omnigent/inner/opencode_executor.py`. Only
text content reaches OpenCode.

**Why deferred.** OpenCode's CLI accepts `--file <path>` (see
`packages/opencode/src/cli/cmd/run.ts`) but plumbing this needs
file materialisation (write each input block to a temp file, pass
the path, clean up afterward). Out of scope for the initial harness
wrap.

**Starting point.** Extend `_latest_user_text` to also return a
list of materialised file paths; thread them into `_build_argv` as
repeated `--file` flags; clean them up in the `finally` block of
`run_turn`.

## 5. Mid-turn interrupt + live message queue

**Gap.** `OpenCodeExecutor.interrupt_session` and
`enqueue_session_message` both return `False`. Cancellation works
via the standard async-gen close path (the runtime cancels the
wrapping HTTP request, `run_turn`'s `finally` block terminates the
subprocess), but there's no out-of-band cancel that doesn't tear
the entire turn down, and there's no way to inject a new user
message mid-turn.

**Why deferred.** The per-turn-subprocess design genuinely lacks
this surface. OpenCode's `serve` HTTP path supports `prompt_async`
queueing and would unlock both features, but switching off the
per-turn subprocess model is the substantial rewrite mentioned in
the executor docstring ("HTTP / SSE transport is a natural
follow-up once the contract is firm").

**Starting point.** Spawn `opencode serve --port 0 --hostname
127.0.0.1` once per Omnigent session; cache the port. Replace the
per-turn `opencode run` with `POST /session/:id/prompt_async`.
Drive the SSE stream off `GET /global/event` instead of parsing
JSONL from stdout. Wire `interrupt_session` and
`enqueue_session_message` against the server's cancel / prompt
endpoints.

## 6. Token usage / cost reporting

**Gap.** `TurnComplete.usage` is set to `None`. OpenCode's
`step_finish` events carry token counts that we currently drop.

**Why deferred.** Easy follow-up; deferred only because it doesn't
unblock anything else. The cost-advisor already integrates with
other executors that report usage.

**Starting point.** In `_translate_event`, capture token counts off
the `step_finish` payload and accumulate them on the executor
instance; pass the totals into `TurnComplete(usage=...)` at end of
turn. The exact field names live in
`packages/opencode/src/cli/cmd/run.ts`.

## 7. `--continue` flag plumbing

**Gap.** Session resume uses `--session <id>` exclusively. OpenCode
also supports `--continue` / `-c` ("continue the last session"),
which we don't surface as an Omnigent-level affordance.

**Why deferred.** `--session <id>` is strictly more general and
covers every Omnigent use case. `--continue` adds value only for an
interactive operator who opened a session outside Omnigent and
wants to resume it inside — niche enough to defer until requested.

**Starting point.** Add a `HARNESS_OPENCODE_CONTINUE_LAST=1` env
var; honour it in `_build_argv` by emitting `--continue` and
skipping `--session`.

## 8. Test coverage gaps

- **Live web-UI verification.** The `configured_harness_map()`
  daemon hello frame includes `opencode`, but I never started
  `omnigent server` + `omnigent host` + `npm run dev` to visually
  confirm OpenCode shows up in the new-session picker. Probably
  works; would be cheap to verify.
- **Workflow `_apply_databricks_profile_to_opencode` real-creds
  test.** The unit test path stubs the workspace resolver. A
  Databricks-creds-required test would catch real-world drift in
  the gateway endpoint shape (`/serving-endpoints` vs
  `/serving-endpoints/anthropic`).

## Out of scope

- **Subscription-style auth detection** (the
  `_SUBSCRIPTION_AUTH_HARNESSES` set in `omnigent/spec/omnigent.py`).
  OpenCode's `opencode auth login` is per-provider, not a single
  subscription, so it doesn't fit that mental model.
- **`ucode` integration** (`_UCODE_HARNESS_CONFIGS` in
  `omnigent/runtime/workflow.py`). The ucode path pre-caches
  gateway state for SDK-wrapping harnesses; OpenCode reads its
  config per-spawn from `OPENCODE_CONFIG_CONTENT`, so the ucode
  cache layer is genuinely redundant for it.
