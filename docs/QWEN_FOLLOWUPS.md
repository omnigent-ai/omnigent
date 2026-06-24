# Qwen Integration Follow-ups

Tracks pending work and known limitations for the Qwen Code harness
(`harness: qwen`, driving `qwen --acp`).

## What works today

- `omnigent run --harness qwen` / `executor.harness: qwen` (alias `qwen-code`).
- ACP executor: streaming turns, system-prompt folding, session-not-found
  reset, missing-binary handling.
- **Permission gating** (`session/request_permission`): routed through
  Omnigent's TOOL_CALL policy + human-consent elicitation
  (`_decide_permission`), mirroring claude-sdk — a hard policy DENY rejects,
  otherwise the user is asked; default-deny on policy-ASK with no handler.
  Standalone/test use (no bridges wired) falls back to allow.
- `omnigent setup` → **Qwen Code** row: installs the CLI and guides auth
  (env vars or interactive `/auth`).
- Auth via the CLI's own ambient credentials (see Auth model below).
- **Provider / gateway routing (clean env).** A spec `auth:` / `providers:`
  entry is translated to `HARNESS_QWEN_GATEWAY_*` vars and the executor exports
  `OPENAI_BASE_URL` / `OPENAI_API_KEY` (from the gateway's bearer-token command,
  run once at session start) / `OPENAI_MODEL` into the `qwen --acp` subprocess.
  Verified end-to-end against an OpenAI-compatible endpoint. **Caveat:** this is
  authoritative only when qwen has no conflicting ambient `~/.qwen/settings.json`
  — see Pending work for the precedence limitation.
- **Cost / token tracking.** Per-turn token usage is parsed from qwen's ACP
  stream and emitted on `TurnComplete.usage` (and fed to the cost observer).
  qwen rides usage out-of-band on an `agent_message_chunk` whose text is empty
  and whose `_meta.usage` carries `{inputTokens, outputTokens, totalTokens,
  thoughtTokens, cachedReadTokens}` (qwen-code `emitUsageMetadata`). The
  executor sums these across a turn's internal model calls and splits
  `cachedReadTokens` out of `input_tokens` (qwen's `inputTokens` is cache-
  inclusive; cost wants the non-cached portion) — see `_accumulate_usage`.
  Verified end-to-end against a live `qwen --acp` turn.
- **OS sandbox.** When the spec's `os_env.sandbox` is not `none`, the whole
  `qwen` process tree is wrapped in the platform sandbox (bwrap / seatbelt) at
  spawn (`_sandbox_launch_path`), confining qwen's own file/shell tools to the
  spec's read/write roots — an OS-level guarantee independent of the per-tool
  permission gate.

## Pending work

Functionality not yet supported, by priority. (How to build each lives in code
comments; this is the *what*, not the *how*.)

### High

- [ ] **In-session model selection.** The model is fixed at `session/new` and
  the session is reused across turns, so switching models mid-session (`/model`)
  has no effect. Supporting it means re-creating the ACP session on a model
  change (or passing a per-turn model if qwen accepts one).
- [ ] **Context status.** Per-turn context *consumed* is now available
  (qwen's `_meta.usage.totalTokens`, surfaced via cost/token tracking — see
  What works today), but the executor still overrides no `max_context_tokens`,
  so the UI's context meter has the numerator without the denominator. Report
  the model's context-window *limit* (a model-capability lookup, not in the ACP
  stream) so the meter can show used/total.
- [ ] **Native TUI variant (`native-qwen`).** Attach to the live `qwen` terminal
  (like `pi-native`) for a fully interactive experience instead of a piped turn.

### Medium

- [ ] **Provider routing: settings.json precedence + token refresh.** The
  base injection now works (see What works today), but two gaps remain before
  it's robust on a developer machine:
  - **Ambient settings win.** qwen prefers a user-level `~/.qwen/settings.json`
    (`security.auth.selectedType` + `modelProviders`) over the injected
    `OPENAI_*` env vars, so on a host where someone ran `qwen /auth`, the spec's
    gateway is silently ignored. qwen exposes no config-dir flag, so making the
    gateway authoritative needs HOME / config-dir isolation for the subprocess.
  - **No token refresh.** The bearer token is snapshotted once at session start;
    qwen has no refresh hook, so a short-lived rotating token (Databricks
    gateway) can expire over a long session. Static keys / stable gateways are
    unaffected.
- [ ] **Databricks path.** Verify the `databricks-*` profile route end-to-end
  (the env plumbing exists; only the OpenAI-compatible gateway has been tested).
  The profile route derives the base URL + auth from **ucode state**, so it
  depends on ucode provisioning a `qwen` agent for the workspace. To test:
  - *Quick (no ucode):* point a gateway straight at Databricks' OpenAI-compatible
    serving endpoint — `gateway_base_url = https://<host>/serving-endpoints`,
    `gateway_auth_command = databricks auth token --profile <p> --output json |
    jq -r .access_token`, `model = <served-endpoint-name>` — run a turn from a
    **clean `HOME`** (so `~/.qwen/settings.json` can't take precedence).
  - *Full route:* spec with `executor.profile: <db-profile>` (or a
    `databricks-*` model), then `omni run`; confirm the runner log's
    `qwen gateway routing:` line shows the Databricks base URL + profile.
- [ ] **Omnigent tools.** Qwen can only call its own built-in tools; tools
  defined by Omnigent aren't exposed to it (so they can't be invoked or
  recorded). Permission gating on qwen's *own* tool calls already works.
- [ ] **File I/O execution / content recording.** Qwen performs file reads and
  writes with its own tools (we don't advertise `clientCapabilities.fs`), so
  Omnigent never executes the I/O and can't record the file content or run
  TOOL_RESULT-phase content policy on it. Note the gaps that are *already*
  closed: the TOOL_CALL request is gated (permission + policy) when qwen asks,
  and the ops are confined when `os_env.sandbox` is set (see What works today).
  What's missing is Omnigent-side execution/recording of the I/O itself —
  advertise `clientCapabilities.fs` + re-add `fs/read_text_file` /
  `fs/write_text_file` handlers to route it through Omnigent.

> LLM-phase policy (`PHASE_LLM_REQUEST` / `PHASE_LLM_RESPONSE`) is intentionally
> out of scope: qwen's model calls happen internally over ACP and are opaque to
> us. Only tool-call-phase policy is feasible, and it is wired.

### Low

- [ ] **More attachment types.** Text files and images now reach the agent;
  still unsupported are binary documents (PDF, etc.) and audio input.
- [ ] **Session resilience:** cancel a turn mid-flight, recover when the `qwen`
  subprocess crashes, and resume a session across separate runs.
- [ ] **Vision/audio quality** depends on the model: text-only routes (e.g.
  `qwen3-coder:free`) can't see forwarded images. Worth surfacing model
  capability to users picking an agent.

## Known limitations & behavior

### Model capability vs. file attachments

Tool-calling reliability depends on the model. Weak/free routes (notably
`qwen/qwen3-coder:free`) **lose the tool-calling thread when a message carries a
file attachment**: instead of emitting a structured tool call (which would reach
our `session/request_permission` gate), they narrate the shell command as prose
(e.g. printing `Command: rm …` as text). The omni run is deterministic about
this — every `input_file` turn skips policy/elicitation; every text-only turn
reaches them. `qwen3-coder-plus` keeps tool-calling across the same prompts.

Mitigation: `_text_from_blocks` fences inlined file content with a labeled
`--- attached file: <name> ---` header/footer so the model reads it as an
attachment, not instructions (bare-appending raw content reproduced the
prose-narration leak even on `:free`). This reduces but does not eliminate the
fragility — for reliable tool use with attachments, prefer a stronger model.

### Auth model

Qwen has **no CLI login** — its `auth` subcommand was removed (`qwen login`
doesn't exist; `qwen auth status` prints "removed" and exits 0). Auth is:

- **Headless / ACP:** env vars — `OPENAI_API_KEY` + `OPENAI_BASE_URL` +
  `OPENAI_MODEL`, or `BAILIAN_CODING_PLAN_API_KEY`, or `OPENROUTER_API_KEY`.
- **Interactive:** run `qwen` and use `/auth` (API key or Alibaba Cloud
  Coding Plan), persisted under `~/.qwen/`.

Qwen OAuth was discontinued 2026-04-15; the installed CLI may still mention it
(version skew), but the service is gone. The `HarnessInstallSpec` deliberately
leaves `login_args` / `logout_args` / `status_args` unset so
`harness_cli_logged_in/login/logout` stay no-ops for qwen.

### ACP constraints

- Qwen runs its own tools internally (not yet bridged — see Pending work).
- Qwen assigns its own `sessionId`; ours is a hint.
- ACP has no system-prompt field, so the spec `prompt:` is folded into the
  first user turn.
- Server-initiated requests are dispatched by method: `request_permission`
  goes through the policy + elicitation gate (see What works today); everything
  else (including `fs/*`) → JSON-RPC method-not-found. We do **not** advertise
  `clientCapabilities.fs` in `initialize`, so qwen never delegates file ops to
  us — it uses its own file tools. (fs delegation handlers were removed as dead
  code; re-add them with the capability — see Pending work.)

## Reference

### ACP session lifecycle (`qwen --acp`, JSON-RPC over NDJSON)

1. `initialize` — capability handshake (once per subprocess).
2. `session/new { cwd, mcpServers }` — server returns its own `sessionId`.
3. `session/prompt { sessionId, prompt }` — streaming `session/update`
   notifications flow back; the final response resolves the request.
4. The subprocess is kept alive across turns (no per-turn respawn).

### Model override

Spec model → provider default → catalog default; `/model` overrides via
`HARNESS_QWEN_MODEL`.

### Env vars consumed by the harness wrap

`HARNESS_QWEN_MODEL`, `HARNESS_QWEN_CWD`, `HARNESS_QWEN_PATH`,
`HARNESS_QWEN_OS_ENV`. (Gateway/Databricks vars are computed but not yet
consumed — see Pending work. No skills-bridge vars are emitted.)
