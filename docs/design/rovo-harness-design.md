<!--
Design rationale for adding Rovo Dev (`acli rovodev`) as an Omnigent harness.
Derived entirely from this repo's code/docs + a live protocol probe of the
Rovo Dev CLI. Authored before any implementation, to put the reasoning on record.
-->

# Design: Rovo Dev as an Omnigent harness (from-scratch rationale)

**Status:** implemented (v1) — pending review
**Author:** Shubham (with Rovo Dev)
**Scope:** Add `acli rovodev` as a first-party Omnigent harness (`rovo-cli`,
alias `rovo`), selectable like `claude-sdk` / `codex` / `pi` / `openai-agents`.

> Provenance note: This design is derived from the Omnigent repo itself (the
> documented harness registry, the `Executor`/`ExecutorAdapter` contract, and the
> `codex_executor.py` subprocess-JSON-RPC precedent) plus a live probe of the
> Rovo Dev CLI's `acp` mode. An in-flight community PR (#88, Cursor) independently
> adds a different CLI via the same ACP approach; we treat it only as
> after-the-fact corroboration and a mergeability checklist, **not** as the source
> of this architecture. See "Relationship to PR #88" at the end.

---

## 1. Goal

Let an Omnigent user run Rovo Dev through Omnigent the same way they run Claude
Code / Codex / Pi — gaining Omnigent's shared session model, policies,
collaboration, and multi-agent composition — by selecting a `rovo` harness in:

- spec YAML (`executor.harness: rovo`),
- `--harness rovo` / `--model …`,
- the REPL `/model` command,
- sub-agent specs (mixed-harness orchestrators like Polly),
- the web-UI harness picker.

---

## 2. How Omnigent harnesses work (established by the repo, not by any PR)

Three in-repo facts define the entire extension surface:

### 2.1 The harness registry is a documented name→module map
`omnigent/runtime/harnesses/__init__.py` is, by its own docstring, *"just the
registry."* It maps the value of `spec.executor.harness` to a module that
exports a zero-arg `create_app() -> FastAPI`:

```python
_HARNESS_MODULES: dict[str, str] = {
    "claude-sdk": "omnigent.inner.claude_sdk_harness",
    "codex":      "omnigent.inner.codex_harness",
    "pi":         "omnigent.inner.pi_harness",
    "codex-native": "omnigent.inner.codex_native_harness",
    ...
}
```
The docstring states the contract plainly: *"The harness IS an HTTP service
speaking the same Pydantic models AP serves to external clients."* So adding a
harness = add one registry entry pointing at a module with `create_app()`.

### 2.2 `create_app()` is trivial; all plumbing lives in `ExecutorAdapter`
Every existing harness factory is ~5 lines. e.g. `codex_native_harness.py`:

```python
def create_app() -> FastAPI:
    adapter = ExecutorAdapter(executor_factory=_build_codex_native_executor)
    return adapter.build()
```

`ExecutorAdapter` (`omnigent/runtime/harnesses/_executor_adapter.py`) is a
`HarnessApp` that *"drives any inner `Executor` instance."* It owns: lazy
executor construction via `executor_factory` (so heavy init happens on first
turn, not at boot), the FastAPI routes, SSE translation (`_translate_event`),
the policy evaluator, the tool-dispatch bridge, interrupt handling, and
`close()` on shutdown. **None of that has to be re-implemented per harness.**

### 2.3 The real work is one `Executor` subclass
`omnigent/inner/executor.py` defines the contract. A harness implements:

- `async run_turn(messages, tools, system_prompt, config) -> AsyncIterator[ExecutorEvent]`
  — the core loop, yielding events.
- capability flags: `supports_streaming()`, `supports_tool_calling()`,
  `handles_tools_internally()`, `supports_live_message_queue()`, …
- optional lifecycle: `interrupt_session()`, `enqueue_session_message()`,
  `close_session()`, `close()`.

And it yields these `ExecutorEvent`s (all already defined in `executor.py`):
`TextChunk`, `ReasoningChunk`, `ToolCallRequest`, `ToolCallComplete`,
`TurnComplete(response, usage, …)`, `TurnCancelled`, `ExecutorError`.

**Conclusion:** the from-scratch task is "write a `RovoExecutor(Executor)` and a
5-line `create_app()`, then register it." This conclusion comes purely from
§2.1–§2.3.

---

## 3. Choosing the integration surface for Rovo (driven by the Rovo CLI itself)

`acli rovodev --help` lists multiple run modes. The relevant ones:

| mode | what it is | fit for a harness? |
|------|------------|--------------------|
| `run` | interactive TUI | ✗ would need a tmux terminal bridge (the heavy `*-native` path) |
| `legacy` | non-TUI CLI | △ scriptable but not a clean streaming RPC |
| `serve PORT` | HTTP server mode | △ viable, but a network port to manage |
| `acp` | **Run Rovo Dev as an ACP server (stdio)** | ✓ structured JSON-RPC stream, no terminal |
| `lsp` / `gui` / `review` | other surfaces | ✗ not general agent turns |

**`acli rovodev acp` is the obvious choice** because it gives a structured,
streaming, stdio JSON-RPC session — which maps directly onto the `Executor`
event model — and avoids the interactive-terminal (tmux) layer entirely.

### 3.1 There is already an in-repo precedent for "spawn a CLI, speak JSON-RPC over stdio"
`omnigent/inner/codex_executor.py` does exactly this today via
`_CodexAppServerSession`: it spawns the Codex CLI as a subprocess and talks a
**JSON-RPC line protocol** to its "app server" — see `_request()`,
`_send_message()`, `_reader_loop()`, `_iter_stream_lines()`, plus
`interrupt_turn()` / `enqueue_message()`. ACP is just a different JSON-RPC
dialect over the same transport, so the Rovo executor follows a structurally
identical shape that the codebase already proves out.

### 3.2 Why not the tmux/native path
`omnigent claude` / `*-native` harnesses wrap an interactive CLI in a locked-down
tmux pane (`omnigent/inner/terminal.py`). We deliberately avoid that for Rovo
because (a) Rovo offers a non-terminal `acp` mode, and (b) the tmux layer carries
ergonomic/operational costs (e.g. clipboard lockdown) that ACP sidesteps.
Accordingly, Rovo is **not** a `NATIVE_HARNESSES` member in
`harness_aliases.py` — it owns an ACP session, not an on-disk TUI transcript,
which puts it in the same category as the SDK harnesses for fork/resume purposes.

---

## 4. Protocol facts — verified by live probe (not assumed)

A throwaway stdio probe against `acli rovodev acp` (JSON-RPC 2.0) confirmed the
exact shapes the executor must handle:

**Handshake — `initialize` →**
```json
{"result":{"protocolVersion":1,
  "agentCapabilities":{"loadSession":true,
    "mcpCapabilities":{"http":true,"sse":true},
    "promptCapabilities":{"image":false,"audio":false,"embeddedContext":false}},
  "authMethods":[{"id":"product-login","name":"Rovo Dev Login",
    "description":"Run `acli rovodev auth login` in your terminal"}]}}
```

**`session/new` →** returns a `sessionId` (UUID) and an `availableModels` list
(`"Claude Sonnet 4.6"`, `"Claude Sonnet 4.5"`, `"Claude Haiku 4.5"`,
`"Claude Opus 4.8"`, …). Model ids are human-readable display names.

**`session/prompt` (the turn) →** streams `session/update` notifications and then
resolves the request with a `stopReason`:
```json
{"method":"session/update","params":{"sessionId":"…",
  "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"P"}}}}
{"method":"session/update","params":{"sessionId":"…",
  "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"ONG"}}}}
{"id":3,"result":{"stopReason":"end_turn"}}
```

Notable capability findings:
- **`loadSession: true`** → session resume is supported (informs `close_session`
  / future fork handling).
- **`mcpCapabilities.http/sse: true`** → spec-declared Omnigent tools can be
  bridged into Rovo via MCP-over-ACP (`session/new` `mcpServers`). This is a
  first-class capability, so tool bridging is in-scope rather than a limitation.
- **`promptCapabilities`** currently advertises no image/audio → multimodal input
  is out of scope for v1 (text prompts only), matching the probe.

---

## 5. Implementation (as built, repo-native naming)

Following the repo's dominant `codex_executor.py` / `codex_harness.py`
convention (not any external naming):

### New files (`omnigent/inner/`)
1. **`rovo_acp.py`** — a minimal ACP client over an asyncio subprocess: framed
   JSON-RPC read/write loop, `initialize`, `session/new`, `session/set_model`,
   `session/prompt`, `session/cancel`, and an async iterator of `session/update`
   notifications. Also auto-allows `session/request_permission` requests (replies
   with `{"outcome": {"outcome": "selected", "optionId": <allow>}}`), since the
   meta-harness governs approvals at the Omnigent layer. Mirrors the transport
   shape of `_CodexAppServerSession`.
2. **`rovo_executor.py`** — `RovoExecutor(Executor)`:
   - `run_turn`: spawn/reuse one ACP session per Omnigent session; send
     `session/prompt`; map `agent_message_chunk` → `TextChunk`, any
     reasoning/thought updates → `ReasoningChunk`, tool-call updates →
     `ToolCallRequest`/`ToolCallComplete`; resolve `stopReason` → `TurnComplete`.
   - **model selection**: `model = cfg.model or self._model_override` (per-request
     `/model` wins over the spec default, mirroring `codex_executor.py`); applied
     via `session/set_model` after session start (skipped if already current).
     `session/new` returns a nested `models` object
     (`{availableModels:[{modelId,name}], currentModelId}`) which the session
     parses into `available_models` + `current_model_id`.
   - capability flags: `supports_streaming() = True`,
     `handles_tools_internally() = True` (Rovo runs its own agent loop),
     `supports_live_message_queue() = False` (ACP has no mid-turn steer).
   - `interrupt_session` → ACP `session/cancel`; `close_session` → end the ACP
     subprocess; `close` → tear down all sessions.
   - persistent ACP session per conversation (cached, like the codex executor).
3. **`rovo_harness.py`** — `_build_rovo_executor()` reading env-var config
   (model, cwd, config-file, site-url, sandbox) + the 5-line `create_app()` =
   `ExecutorAdapter(executor_factory=_build_rovo_executor).build()`.

### Edits
4. `omnigent/runtime/harnesses/__init__.py` — add
   `"rovo": "omnigent.inner.rovo_harness"` (and/or `"rovo-cli"`).
5. `omnigent/harness_aliases.py` — add a user-facing alias if we keep a separate
   canonical id; do **not** add to `NATIVE_HARNESSES`.
6. Web-UI harness picker — one line in `ap-web/src/lib/agentLabels.ts`
   (`"rovo-cli": "Rovo Dev"` in `BRAIN_HARNESS_LABELS`) so `rovo` is selectable in
   the New-Chat / Switch-Agent fly-out. Verified with `tsc -b` and the picker
   component tests.

### Config (env-var, mirroring `codex_harness.py`'s `HARNESS_CODEX_*` pattern)
- `HARNESS_ROVO_MODEL`, `HARNESS_ROVO_CWD`, `HARNESS_ROVO_CONFIG_FILE`
  (default `~/.rovodev/config.yml`), `HARNESS_ROVO_SITE_URL`,
  `HARNESS_ROVO_PATH` (override `acli` location), sandbox mapping from `os_env`.

### Auth
Rovo auth is handled by the CLI itself (`acli rovodev auth login`, probe shows
`authMethods: product-login`). The harness does not manage credentials; if
unauthenticated, surface the CLI's guidance as an `ExecutorError`.

---

## 6. Tests (matching the repo's bar)

- `tests/inner/test_rovo_acp.py` — handshake / session/new (nested `models`) /
  prompt streaming / cancel, against a fake stdio server (no real CLI).
- `tests/inner/test_rovo_executor.py` — `session/update` → `ExecutorEvent`
  mapping, persistent-session reuse, `stopReason` → `TurnComplete`, permission
  auto-allow, and **model selection** (parse nested models; `config.model` and
  spec-default precedence; config-wins-over-spec; no-model keeps Rovo default).
- `tests/inner/test_rovo_harness.py` — env-var config flow → executor args.
- `tests/inner/test_rovo_live_e2e.py` — gated live E2E (simple turn + multi-turn
  with tools) that **skips unless `ROVO_LIVE=1`** and `acli` is present, so CI
  without the CLI stays green.
- `tests/inner/_fake_acp_server.py` — the fake stdio ACP server backing the unit
  tests (returns the real nested `models` shape; handles `session/set_model`).

Local gate before PR: `uv run pre-commit run --all-files` (ruff) and, since the
picker touches the frontend, `cd ap-web && npm run build` / `tsc -b`.

---

## 7. Known limitations / out-of-scope for v1
- **Bridging Omnigent's spec-declared tools into Rovo (MCP-over-ACP)** — the main
  follow-up. Rovo advertises `mcpCapabilities.http/sse`, so spec tools can be
  bridged via `session/new`'s `mcpServers` later. v1 uses Rovo's own native tools.
- Multimodal prompt input (image/audio) — Rovo's `promptCapabilities` currently
  advertises none; text-only for v1.
- Token usage — ACP exposes none in the turn result, so `TurnComplete.usage =
  None` (the `Executor` contract allows this).
- Mid-turn steering — ACP has no live message queue, so
  `supports_live_message_queue() = False`.
- Fork/resume parity — `loadSession: true` makes this feasible later; v1 targets
  a fresh ACP session per conversation with clean `close_session`.

> Implemented in v1 (not limitations): multi-turn chat, streaming, Rovo's native
> tool use with auto-allowed permissions, **model selection** (`session/set_model`),
> and the web-UI harness picker entry.

---

## 8. Risks
- **ACP is young / shapes may shift** — mitigated by keeping `rovo_acp.py` thin
  and unit-tested against recorded fixtures from the probe.
- **`acli` not installed in CI** — handled by gating the E2E and keeping unit
  tests CLI-free.
- **Concurrent overlapping work (PR #88)** — both edit the registry/aliases and
  picker. Mitigation: keep this PR independent; rebase and resolve the small
  shared-file conflicts if #88 merges first.

---

## Appendix — Relationship to PR #88 (Cursor harness)

PR #88 (`feat(harnesses): add cursor first-party harness`, by a maintainer) adds
Cursor via ACP. It is **corroboration**, not the basis of this design:

- The architecture here is fully derivable from §2 (repo registry + `Executor`/
  `ExecutorAdapter` contract) and §3.1 (the `codex_executor.py` subprocess-
  JSON-RPC precedent) — all in-repo before #88.
- The ACP choice (§3) comes from the **Rovo CLI's own `acp` mode** and the live
  probe (§4), not from #88.
- Where the repo+probe disagree with #88, we follow the repo+probe: e.g. #88
  notes ACP tool-bridging as a *limitation* for Cursor, but the probe shows Rovo
  advertises `mcpCapabilities.http/sse`, so tool bridging is **in-scope** here.
- #88's genuine, non-architectural value: (a) a maintainer independently chose
  ACP for the same class of integration (confidence), and (b) it illustrates the
  PR-review/mergeability bar (checklist, tests, `tsc -b`), some of which also
  comes directly from `CONTRIBUTING.md` and the PR template.

If #88 did not exist, this design would be unchanged.
