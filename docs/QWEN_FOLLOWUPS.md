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
- **Context status.** The UI context meter shows used/total for qwen. The
  numerator (per-turn context consumed) comes from `_meta.usage.totalTokens`
  via cost/token tracking above; the denominator (the model's context-window
  *limit*) comes from a curated Qwen lookup in `get_model_context_window`
  (`_QWEN_CONTEXT_WINDOWS`) — qwen models are absent from litellm and the MLflow
  catalog, so without it they fell back to the wrong 128K default
  (qwen3-coder-plus is 1M). A spec's `executor.context_window` still overrides;
  unrecognized qwen models keep the 128K fallback.
- **In-session model selection (`/model`).** Switching models mid-session
  works. The model is fixed in the `qwen --acp` subprocess env
  (`HARNESS_QWEN_MODEL`) at spawn, so on a `/model` change the runner's
  `HarnessProcessManager` respawns the harness with the new value — a fresh
  `QwenExecutor` then opens a new `session/new` carrying the new model. Context
  survives the respawn because the first turn of the new session replays the
  prior conversation (see History replay below).
- **History replay on a fresh ACP session.** When the `qwen --acp` subprocess
  is (re)spawned — first turn, a `/model` switch, or a `Session not found`
  reset — qwen holds none of the earlier conversation (it lived in the dead
  process). `run_turn` normally sends only the latest user turn, so the first
  turn of any fresh session folds the prior transcript into the prompt as a
  labeled `Conversation so far:` block (`_history_prefix`), mirroring
  `ClaudeSDKExecutor._build_prompt`. Keeps a mid-conversation model switch from
  dropping the thread. (Same fix applied to the goose ACP harness.)
- **OS sandbox.** When the spec's `os_env.sandbox` is not `none`, the whole
  `qwen` process tree is wrapped in the platform sandbox (bwrap / seatbelt) at
  spawn (`_sandbox_launch_path`), confining qwen's own file/shell tools to the
  spec's read/write roots — an OS-level guarantee independent of the per-tool
  permission gate.
- **File I/O delegation (`fs/*`).** When an `os_env` is configured, the executor
  advertises `clientCapabilities.fs` in `initialize`, so qwen routes its file
  reads/writes back to us as `fs/read_text_file` / `fs/write_text_file` requests
  (qwen's `AcpFileSystemService` swaps in only when the capability is set). The
  handlers execute the I/O through the Omnigent `OSEnvironment`, so the spec's
  sandbox read/write roots are enforced at the Python layer — and the bytes flow
  through Omnigent rather than qwen touching disk directly. Disabled (qwen uses
  its own tools) when there's no `os_env` or it's a `fork` env (a forked tree's
  path would diverge from the qwen subprocess cwd). Binary/non-UTF-8 reads are
  refused; missing-file reads map to qwen's ENOENT code. (Same fix applied to
  the goose ACP harness.) See the Pending item below for what's still out of
  scope (event recording / TOOL_RESULT-phase content policy).

## Pending work

Functionality not yet supported, by priority. (How to build each lives in code
comments; this is the *what*, not the *how*.)

### High

- [x] **Native TUI variant (`qwen-native` / `native-qwen`).** Implemented — the
  live `qwen` TUI runs in a runner-owned tmux pane embedded in the web UI, driven
  by `omnigent qwen`. Unlike the goose/cursor `tmux send-keys` native harnesses,
  it uses qwen's built-in remote-control protocol: web-UI turns are appended to
  qwen's `--input-file` (a `{"type":"submit"}` line, routed through the same
  `submitQuery` path the keyboard uses, so it renders in the TUI), and the
  transcript is mirrored back by tailing qwen's structured `--json-file` event
  stream (Anthropic stream-json shape). Interrupt/stop still go through the pane
  (`Escape` / kill) since the input-file watcher has no interrupt command. See
  `docs/QWEN_NATIVE_DESIGN.md`. **Still a follow-up (PR2):** usage parsing from
  the `result`/`assistant` events is not yet emitted on `TurnComplete.usage`
  (see the status-line item below for the model/ring/cost consequences).

- [x] **Tool-approval elicitation card (TUI → web).** Implemented — qwen's
  in-terminal tool-approval prompt now also renders as an approval card in the
  web chat, and answering either surface resolves the other. qwen emits a
  structured `{"type":"control_request","request":{"subtype":"can_use_tool",
  "tool_name","tool_use_id","input"},"request_id"}` on `--json-file` **and**
  accepts a `{"type":"confirmation_response","request_id","allowed"}` on
  `--input-file`, *coexisting* with its own TUI prompt (whichever answers first
  wins; qwen's `dual-output.md` confirms `control_request` is emitted whenever a
  tool needs approval — the earlier "default mode doesn't emit these" note was
  wrong).
  - **Mirror:** `omnigent/qwen_native_permissions.py` —
    `supervise_qwen_approval_mirror` tails the *same* `--json-file` the
    transcript forwarder reads (seeded at EOF so only new prompts park), POSTs
    each `can_use_tool` to the server's `qwen-permission-request` hook, and on
    the web verdict writes `confirmation_response` to the input file (no
    keystrokes). It's the structured analog of cursor-native's pane-scraping
    mirror. Wired alongside the forwarder under one supervised task in
    `runner/app.py::_auto_create_qwen_terminal` (`_supervise_qwen_native_bridges`).
  - **Server hook:** `POST /v1/sessions/{id}/hooks/qwen-permission-request`
    (`qwen_permission_request_hook`, modeled on the cursor hook) publishes the
    standard `response.elicitation_request` (`policy_name=qwen_native_permission`,
    `phase=pre_tool_use`) and parks via `_publish_and_wait_for_harness_elicitation`.
    This always surfaces a card whenever the TUI prompts — the explicit goal —
    rather than routing through `/policies/evaluate` (which would auto-resolve
    and skip the card when no TOOL_CALL policy matches qwen's tool names).
  - **Loser release:** qwen emits a `control_response` for a `request_id`
    whether the TUI or an external `confirmation_response` answered. The mirror
    watches for it: if it lands while the web card is still parked (TUI answered
    first), it POSTs `external_elicitation_resolved` to clear the card and skips
    the stale `confirmation_response`; if the card answered first, the task is
    already done and the `control_response` just cleans up. Still worth a live
    E2E to confirm timing under a real `qwen --acp` turn.

- [ ] **Composer status line: real model + context ring (Web UI).** For
  native-qwen the composer's model/effort chip is currently **hidden** (web UI
  flag `nativeVendorOwnsModel` in `chatStore.sessionBindingPatch` →
  `ComposerStatusLine` in `web/src/pages/ChatPage.tsx`). It was showing the
  bound spec's *default* model (`claude-sonnet-4-6`) because the qwen-native-ui
  spec sets no model and qwen picks its model inside the vendor TUI (OpenAI-compat
  env / qwen's own `/model`), so Omnigent's `llmModel` was a misleading default.
  Hiding it is the interim; the real fix is to **surface qwen's actual model**
  (and effort/approval-mode if meaningful). The data is already on qwen's
  `--json-file` stream — `assistant` message events carry `message.model` (e.g.
  `openai/gpt-oss-120b:free`) and the `system/session_start` event carries model
  metadata. The forwarder (`omnigent/qwen_native_forwarder.py`) could parse it
  and report it onto the session so the chip reflects qwen's reality.
  - **Context ring + cost tracking also missing**, same root cause: native-qwen
    doesn't yet parse/forward token usage, so `tokensUsed` / `contextWindow` stay
    null (the ring renders only when `contextWindow > 0 && tokensUsed != null`)
    and the session cost stays $0 (cost is derived from per-turn usage × model
    price). The usage *is* on the stream, though — verified live (`qwen`
    v0.18.2): each turn's final `assistant` event carries `message.usage`
    (`{input_tokens, output_tokens, cache_read_input_tokens, total_tokens}`), so
    the forwarder could parse it and POST `external_session_usage`. The ACP
    `qwen` harness already does this — see "Cost / token tracking" in *What works
    today* (`_accumulate_usage`); native-qwen needs the equivalent off the
    `--json-file` stream. Parse `result.usage` (`input_tokens` / `output_tokens`
    / `cache_read_input_tokens` / `total_tokens`) in
    `omnigent/qwen_native_forwarder.py`, split `cache_read_input_tokens` out of
    `input_tokens` (qwen's `input_tokens` is cache-inclusive; cost wants the
    non-cached portion), and report it onto the session so the cost observer +
    context ring pick it up; the context-window *limit* comes from the curated
    `_QWEN_CONTEXT_WINDOWS` lookup. One usage-parsing change feeds the model
    chip, the ring, and cost together.

- [x] **Restore qwen's TUI history on resume.** `omni qwen --resume <conv_id>`
  used to relaunch a **blank** `qwen` TUI (only the web chat kept history, via the
  forwarder). Fixed, using the **same `external_session_id` convention as
  claude-/codex-/pi-native** (so it's consistent and fork-capable):
  `_auto_create_qwen_terminal` persists the qwen session id on the Omnigent
  session (`_persist_qwen_external_session_id` → `PATCH /v1/sessions/{id}`), reads
  it back from the snapshot (`launch_config.external_session_id`) on the next
  launch, and it's stamped as `omnigent.fork.source_external_session_id` for fork
  history carry-over. qwen is cleaner than claude/codex here — it lets us *assign*
  the id via `--session-id`, so we mint a deterministic one
  (`qwen_session_id_for_conversation`, UUIDv5 of the `conv_id`) up front instead
  of capturing a vendor-generated id, and a failed persist self-heals (the id is
  recomputable). Launch is fresh `--session-id <id>` the first time, `--resume
  <id>` once qwen has an on-disk recording — the recording check
  (`qwen_session_recording_exists`, scoped to the launch workspace's qwen project
  slug at `~/.qwen/projects/<slug>/chats/<id>.jsonl`) is the `--resume` guard,
  since `--resume` on an id not recorded *under that cwd* shows qwen's blocking
  "No saved session found" screen — qwen resolves `--resume` per-project, so the
  check must be workspace-scoped, not a cross-project glob (also keeps
  never-messaged / pre-convention sessions on the clean fresh path). **No forwarder change
  needed:** verified that on `--resume` qwen restores history into the TUI from
  its own checkpoint and emits *only new* events to `--json-file`, so the
  transcript is never re-mirrored — qwen sidesteps the double-mirror problem that
  forced goose-native to start fresh.

### Medium

- [x] **Compaction via `/compact` (web → TUI), with spinner + divider.**
  Implemented, mirroring cursor-native PR #1259 — the web composer's `/compact`
  now drives qwen's `/compress` in the TUI, with a "Compacting conversation…"
  spinner that resolves to the "Conversation compacted" divider when qwen
  actually finishes. Works for both explicit `/compact` and auto-compaction.
  - **Server (existing, harness-agnostic):** `/compact` → forwards `{"type":
    "compact"}` to the bound runner; a 200 means the control was handled in the
    terminal (server skips its own AP-side compaction, which 400s on the
    LLM-less native pseudo-agent).
  - **Runner (`_handle_qwen_native_compact`):** publishes
    `response.compaction.in_progress` (raises the spinner), submits `/compress`
    via the **input file** (`submit_user_message`), returns 200; on failure
    publishes `response.compaction.failed` (dismisses the spinner) + 503. Unlike
    cursor's bracketed-paste, qwen's input-file `submit` routes through
    `RemoteInputWatcher` → `submitQuery` (the keyboard's own path), which
    processes the slash command directly — no autocomplete-dropdown trap, and no
    `/compress` user bubble on the stream (verified live, `qwen` v0.18.2).
  - **Completion signal — the chat recording, not the stream.** qwen emits **no**
    compression event on the `--json-file` stream (`session_start`'s
    `supported_events` omits it; the green "compressed from…" TUI line is an
    internal `addItem`, never streamed). But it writes a `{"type":"system",
    "subtype":"chat_compression","systemPayload":{"info":{originalTokenCount,
    newTokenCount,compressionStatus}}}` record to its on-disk recording
    (`~/.qwen/projects/<slug>/chats/<id>.jsonl`) the instant compression
    finishes. `supervise_qwen_compaction_mirror` tails that recording (seeded at
    EOF so a resumed session's prior records don't re-fire) and POSTs
    `external_compaction_status` — `completed` on `compressionStatus == 1`,
    `failed` on the `COMPRESSION_FAILED_*` codes (2/3) — which the server
    republishes as `response.compaction.completed/failed`.
  - **Note on the ACP `qwen` harness:** the in-process executor compresses
    internally over ACP and is opaque to us (same boundary as the LLM-phase
    policy exclusion below), so this item is **native-qwen only**.
  - **Follow-up:** the context ring won't shrink after compaction until usage is
    forwarded as `external_session_usage` (see the "Composer status line" item) —
    the recording's `newTokenCount` could feed that.

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
- [ ] **File I/O recording / content policy.** Omnigent now *executes* delegated
  file reads/writes through the `OSEnvironment` (see "File I/O delegation" in
  What works today), so the bytes flow through Omnigent and the sandbox roots are
  enforced. Still missing on top of that: (a) emitting the I/O into Omnigent's
  event stream (ToolCall-style records) so it shows in history, and (b) running
  TOOL_RESULT-phase content policy on the read/written content. Both build on the
  `_handle_fs_read` / `_handle_fs_write` handlers — the byte-level hook now
  exists; this is wiring the recording/policy layers onto it.

> LLM-phase policy (`PHASE_LLM_REQUEST` / `PHASE_LLM_RESPONSE`) is intentionally
> out of scope: qwen's model calls happen internally over ACP and are opaque to
> us. Only tool-call-phase policy is feasible, and it is wired.

### Low

- [ ] **More attachment types.** Text files and images now reach the agent;
  still unsupported are binary documents (PDF, etc.) and audio input.
- [ ] **Session resilience:** cancel a turn mid-flight, recover when the `qwen`
  subprocess crashes, and resume a session across separate runs.
  - *Done in this pass:* dead qwen-native terminals now recreate on attach
    instead of failing 4404, so the embedded pane recovers after a crash or
    deferred-start failure.
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
