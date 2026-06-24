# Kimi Code harness — known follow-ups

The Kimi Code CLI harness landed in #271 with the runtime, CLI,
onboarding, frontend, and gateway-routing wiring complete. This file
tracks the gaps deliberately deferred so they don't get lost. Each item
lists what the gap is, why it was deferred, and a concrete starting
point for whoever picks it up.

## 1. Omnigent-side provider injection (MCP + provider routing)

**Gap.** Two related gaps for the same reason — upstream Kimi Code CLI
has no per-spawn config override flag (no `--config-file`, no
`--mcp-config-file`):

- **Tools.** Spec-declared tools (`tools:` block in the agent YAML) are
  not exposed to the kimi subprocess. `KimiExecutor.run_turn` accepts
  the `tools` argument for ABI parity and logs a one-time warning per
  session.
- **Providers.** A spec that declares any `executor.auth`
  (`{type: provider, name: X}`, `{type: databricks, profile: P}`, or
  `{type: api_key, ...}`) cannot be threaded through to kimi.
  `_build_kimi_spawn_env` raises an `OmnigentError` at spawn-env build
  time rather than silently routing through whatever default kimi
  already had — so users understand why their auth didn't take effect.
  (The kimi builder never calls
  `configure_agent_harness_with_provider` — there is no env-var surface
  to translate a provider into — so the rejection lives in the builder
  itself.)

For v1, both are managed out-of-band:

- Tools: not exposed.
- Providers: configure via `kimi provider add` in
  `~/.kimi/config.toml`, then pin the resulting model id in the agent
  spec.

**Why deferred.** Two paths exist, both substantial:

1. **MCP-via-config-file.** Wait for upstream kimi to grow a
   `--mcp-config-file` (or equivalent stdin-injection mechanism); then
   boot a FastMCP server bound to `127.0.0.1:0` per session and inject
   its URL via env. Plumbing-heavy (~300–500 lines, see the Codex
   `dynamicTools` analogue at ~2300 lines total in
   `omnigent/inner/codex_executor.py`).
2. **ACP-server long-lived process.** Switch off the per-turn
   subprocess to a single `kimi acp` server speaking the Agent Client
   Protocol (https://agentclientprotocol.com/) over stdio. ACP has
   first-class tool registration + cancellation + new-message
   injection. This is the right long-term shape — it also unlocks
   mid-turn interrupt + live message queue (follow-up #5) — but is a
   substantial rewrite.

**Starting point.** Read kimi's `kimi acp` reference; mirror the Codex
App-Server JSONL bridge structure but speak ACP instead. The ACP spec
is at https://agentclientprotocol.com/ and kimi documents its server
under `kimi acp --help`.

## 2. Native TUI launch (tmux-pane parity with `omnigent claude`)

**Gap.** `omnigent kimi` is a discoverability shortcut for
`omnigent run --harness kimi` — it runs Kimi headlessly behind the
standard Omnigent REPL, not the kimi TUI in a tmux pane. There is no
`kimi-native` harness analogous to `claude-native` / `codex-native`.

**Why deferred.** Native TUI integration is a separate piece of work
(~300–500 lines): a `tmux`-based pane manager, a `KimiNativeExecutor`
that wraps the subprocess and bridges input/output through Omnigent's
terminal layer, and the equivalent of `omnigent/claude_native_*.py` /
`omnigent/codex_native_*.py`. Kimi's CLI already supports `kimi acp`
(Agent Client Protocol over stdio), which Zed and JetBrains use for IDE
integration, so the implementation would be easier than
Claude/Codex's were.

**Starting point.** Model on `omnigent/codex_native_executor.py` +
`omnigent/codex_native_harness.py`. Adding `kimi-native` to
`OMNIGENT_HARNESSES`, `_HARNESS_MODULES`, `NATIVE_HARNESSES`, and the
`omnigent.kimi_native_*` analogue file set is the bulk of the work. The
`kimi acp` flag means most of the TUI-rendering plumbing already lives
in Kimi itself.

## 3. Dedicated Kimi glyph

**Gap.** `ap-web/src/components/AgentCard.tsx` falls through to
`BotIcon` for kimi agents. The other CLI harnesses each have their own
SVG glyph under `ap-web/src/components/icons/`.

**Why deferred.** No canonical SVG to copy from Kimi's repo yet.
Trivially small once an asset lands — add a `KimiIcon.tsx`, import it
in `AgentCard.tsx`, and switch the fallback comment to a real branch.

## 4. Multimodal input (incl. video)

**Gap.** Image / file / audio blocks (`input_image`, `input_file`,
`input_audio`) on a user message are dropped with a warning per
`_latest_user_text` in `omnigent/inner/kimi_executor.py`. Only text
content reaches kimi.

**Why deferred.** Kimi advertises **video input** as a first-class
feature (drop a screen recording or demo clip into the chat) — so the
multimodal story here is richer than for the other harnesses, but
plumbing it from Omnigent specs into kimi's CLI needs file
materialisation (write each input block to a temp file, pass the path,
clean up afterward) and a decision on the API surface. Out of scope
for the initial harness wrap.

**Starting point.** Kimi's CLI accepts the file as a positional
argument after the prompt (see kimi's `kimi-command` reference).
Extend `_latest_user_text` to also return a list of materialised file
paths; thread them into `_build_argv`; clean them up in the `finally`
block of `run_turn`.

## 5. Mid-turn interrupt + live message queue

**Gap.** `KimiExecutor.interrupt_session` terminates the active process
but doesn't preserve a queued message; `enqueue_session_message` always
returns `False`. Cancellation works via the standard async-gen close
path (the runtime cancels the wrapping HTTP request, `run_turn`'s
`finally` block terminates the subprocess), but there's no way to
inject a new user message mid-turn.

**Why deferred.** The per-turn-subprocess design genuinely lacks this
surface. Kimi's `kimi acp` long-lived path supports the Agent Client
Protocol — a proper bidirectional stdio protocol with cancel /
new-prompt messages — and would unlock both features, but switching off
the per-turn subprocess model is the substantial rewrite mentioned in
the executor docstring ("HTTP / SSE transport is a natural follow-up
once the contract is firm").

**Starting point.** Spawn `kimi acp` once per Omnigent session; cache
the stdio handles. Replace the per-turn `kimi --print` with a sequence
of ACP `prompt` / `cancel` messages. Drive the event stream off ACP's
`session/update` events instead of parsing JSONL from stdout. Wire
`interrupt_session` and `enqueue_session_message` against the ACP
cancel / prompt endpoints. The ACP spec is at
https://agentclientprotocol.com/.

## 6. Token usage / cost reporting

**Gap.** `TurnComplete.usage` is set to `None`. Kimi's `step_finish`
events (if present in stream-json output) carry token counts that we
currently drop.

**Why deferred.** Easy follow-up; deferred only because the
stream-json output schema is still settling and the kimi-cli docs
don't yet pin the field names. The cost-advisor already integrates
with other executors that report usage.

**Starting point.** Inspect stream-json output via
`kimi --print --output-format stream-json --debug` against a real
session and capture the usage field names. Update `_translate_event`
in `omnigent/inner/kimi_executor.py` to accumulate them on the
executor instance; pass the totals into `TurnComplete(usage=...)` at
end of turn.

## 7. Plan mode + thinking-mode controls in the spec

**Gap.** `KimiExecutor` honours `HARNESS_KIMI_PLAN` and
`HARNESS_KIMI_THINKING` env vars, but there is no spec-level field
that surfaces them on an Omnigent agent YAML. A user wanting to pin
plan mode for a research agent has to export the env var rather than
declare it inline.

**Why deferred.** Adding spec-level fields means updating the
`ExecutorSpec` parser, the workflow spawn-env builder, and the
single-file launcher generator — a larger surface for a relatively
niche feature.

**Starting point.** Add an optional `executor.config.kimi.{plan,
thinking}` block (parsed in `omnigent/spec/parser.py`), thread it
through `_build_kimi_spawn_env` into the env vars.

## 8. Built-in agent specs (`okabe` and friends)

**Gap.** Kimi ships built-in agent specs (`--agent default` /
`--agent okabe`) that customise the system prompt + tool set.
`KimiExecutor` honours `HARNESS_KIMI_AGENT` and `HARNESS_KIMI_AGENT_FILE`,
but there is no surface to select them from an Omnigent spec — users
must export the env var.

**Why deferred.** Same shape as #7 — needs spec parser + workflow
plumbing. Doc-only follow-up until the built-in agent surface is more
broadly used.

**Starting point.** Same pattern as #7 above, with
`executor.config.kimi.agent` / `executor.config.kimi.agent_file`.

## 9. Test coverage gaps

- **Live web-UI verification.** The `configured_harness_map()` daemon
  hello frame includes `kimi`, but I never started `omnigent server`
  + `omnigent host` + `npm run dev` to visually confirm Kimi shows up
  in the new-session picker. Probably works; would be cheap to verify.
- **Workflow `_apply_databricks_profile_to_kimi` real-creds test.**
  The unit test path doesn't exercise the Databricks resolver. A
  Databricks-creds-required test would catch real-world drift in the
  gateway endpoint shape (`/serving-endpoints` vs
  `/serving-endpoints/anthropic`).

## Out of scope

- **Subscription-style auth detection** (the
  `_SUBSCRIPTION_AUTH_HARNESSES` set in `omnigent/spec/omnigent.py`).
  Kimi's `kimi login` is OAuth or a single Moonshot API key, not a
  multi-vendor subscription, so it doesn't fit that mental model.
- **`ucode` integration** (`_UCODE_HARNESS_CONFIGS` in
  `omnigent/runtime/workflow.py`). The ucode path pre-caches gateway
  state for SDK-wrapping harnesses; Kimi reads its config per-spawn
  from `HARNESS_KIMI_CONFIG_CONTENT` (synthesised into a temp
  `--config-file`), so the ucode cache layer is genuinely redundant
  for it.
