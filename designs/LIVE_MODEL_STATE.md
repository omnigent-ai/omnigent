# Live model state: making every surface reflect the running model

Owner: routing MVP. Status: partially implemented (see "Implemented" /
"Deferred" at the end). Companion evidence: live protocol probes against
codex-cli 0.145.0 (`codex app-server`, tmux-driven `--remote` TUI).

## Problem

On a codex-native session with intelligent routing, the rollout file proves
turns ran the routed model (`gpt-5.6-luna`), but:

- the codex TUI `/model` surface shows the thread's startup default
  (`databricks-gpt-5-5`),
- the omnigent web dropdown does not track the routed model, and
- `conversations.model_override` can silently revert to the launch default.

## Where "current model" lives today (evidence)

### Codex-native process side

- Launch pins the model twice: the app-server pins it into the per-session
  `config.toml` (`_pin_codex_config_model`,
  `omnigent/codex_native_app_server.py:205-244`) and the TUI is launched with
  the same value as a CLI override (`-c model="..."` in
  `build_codex_remote_args`, `omnigent/codex_native_app_server.py:2140-2219`).
- A web/routed model change is applied thread-level: the runner threads
  `request.model_override` → `ExecutorConfig.model`
  (`omnigent/runtime/harnesses/_executor_adapter.py:281-285`) and
  `CodexNativeExecutor.run_turn` sends `thread/settings/update` before the
  bare `turn/start` (`omnigent/inner/codex_native_executor.py`, run_turn's
  no-active-turn branch; `_model_effort_overrides` at the bottom of the file).
- The forwarder mirrors TUI→omnigent: `_refresh_model_from_config` reads the
  per-session `config.toml` top-level `model` key (what an in-TUI `/model`
  writes) at subscription (`omnigent/codex_native_forwarder.py:2123-2132`) and
  at **every** `turn/started`
  (`omnigent/codex_native_forwarder.py:2893-2900`), then `_sync_model_change`
  posts `external_model_change` when it differs from the last posted baseline
  (`omnigent/codex_native_forwarder.py:2735-2774`). `thread/settings/updated`
  notifications also feed `forwarder_state.model`
  (`omnigent/codex_native_forwarder.py:2923-2938`, state at `:383-460`).
- The cost-gate hook reads `config.toml` synchronously at tool-gate time
  (`omnigent/codex_native_hook.py:134-150`,
  `read_codex_config_model` in `omnigent/codex_native_bridge.py:277-311`).

### Server side

- `conversations.model_override` (packed into `session_overrides` JSON,
  `omnigent/db/db_models.py:797-802`; entity
  `omnigent/entities/conversation.py:221`) is written by: session create
  (`orchestration.py:5620-5628`), first-message/turn routing
  (`orchestration.py:3774-3790` top-level, `:3743-3756` child), auto-harness
  (`orchestration.py:3653-3655`), and `external_model_change`
  (`_persist_external_model_change`,
  `omnigent/server/routes/_sessions/helpers.py:1901-1952` — dedupes against
  `conv.model_override`, publishes a `session.model` SSE).
- Routing only runs when no model is pinned:
  `_should_route` requires `effective_runner_override is None` for top-level
  sessions (`orchestration.py:3706-3713`).
- The forwarded runner body carries
  `model_override = body.model_override or conv.model_override`
  (`helpers.py:5302-5306`, `orchestration.py:3792-3793`).

### Web UI

- `sessionModelOverride` mirrors the server field
  (`web/src/store/chatStore.ts:379`, hydrated at `:2282`), updated by the
  PATCH path (`:1768-1783`) and by the `session.model` SSE (`:4205-4218`).
- The dropdown/status label resolve through `useResolvedComposerModel`
  (`web/src/pages/ChatPage.tsx:5960-6041`); the router's session pick renders
  as `RoutingDecisionCard` (`web/src/components/blocks/StatusBlocks.tsx:144-300`,
  rendered at `ChatPage.tsx:3048-3060`).

### Claude-native (reference behavior)

- The executor applies a routed switch by typing `/model <m>` and injecting
  the message under one lock
  (`omnigent/inner/claude_native_executor.py:152-189`, dedupe baseline
  `_should_switch_model` at `:190-212`), so the harness's own UI reflects the
  switch; the claude forwarder mirrors it back. This is the model (a) truth
  flow we want for codex.

## Codex 0.145.0 app-server facts (probed live)

Probes: `probe_appserver.py`, `probe_broadcast.py`, `probe_tui.py`
(scratchpad; reproducible against `codex-cli 0.145.0`).

1. `thread/settings/update` exists and **is** the thread-level model switch —
   it requires the `experimentalApi` capability at `initialize`
   (omnigent already sends it, `codex_native_app_server.py:409-414`).
2. It emits a `thread/settings/updated` notification carrying the new
   `threadSettings.model`, and the notification **is broadcast to other
   connected clients that resumed the thread** (verified with two ws
   clients) — i.e. the `--remote` TUI receives it.
3. It does **not** write `config.toml`.
4. `turn/start` has no per-turn model parameter (an extra `model` field is
   silently ignored).
5. The live TUI (tmux probe): the **bottom status bar updates immediately**
   to the new model after a remote `thread/settings/update`
   ("`gpt-5.3-codex default · …`"); the startup banner box stays on the
   launch model (static), and the `/model` picker list does not highlight
   models outside its catalog.

## Root cause of the observed divergence: the config.toml reversion loop

The routed switch is applied thread-level (rollout runs `gpt-5.6-luna`), but
`thread/settings/update` never touches `config.toml`, which still holds the
pinned launch model. Then:

1. Turn N routes → `model_override = luna` persisted
   (`orchestration.py:3779-3783`) → executor `thread/settings/update(luna)` →
   `turn/start`.
2. Forwarder handles `thread/settings/updated` → posts
   `external_model_change(luna)` (deduped server-side, `model_override`
   already `luna`).
3. Forwarder handles `turn/started` → `_refresh_model_from_config` re-reads
   the **stale** `config.toml` (`databricks-gpt-5-5`)
   (`codex_native_forwarder.py:2893-2900`) → `_sync_model_change` posts
   `external_model_change(databricks-gpt-5-5)` → server persists
   `model_override = databricks-gpt-5-5` and publishes `session.model` with
   the default.
4. Turn N+1: `model_override` is non-None (`databricks-gpt-5-5`) so routing
   is skipped ("model already pinned", `orchestration.py:3714-3723`) and the
   executor sends `thread/settings/update(databricks-gpt-5-5)` — the thread
   itself **reverts to the default**.

Net effect: the routed model survives one turn; every surface (TUI status
bar, `/model`, web dropdown, cost gate via the hook's `config.toml` read)
settles back on the launch default — exactly what was observed live.

Secondary gaps:

- **No SSE on routing persist**: routing wrote `model_override` without
  publishing `session.model`, so the web dropdown lagged until reload.
- **Launch race**: the runner's terminal auto-create reads the snapshot's
  `model_override` (`omnigent/runner/native/orchestration.py:711-739`,
  used at `:3458-3473`) — a first message routed after the snapshot read
  launches the TUI pinned to the default. (Benign once the reversion loop is
  fixed: every turn re-applies `ExecutorConfig.model` via
  `thread/settings/update`, so the running thread converges on the routed
  model on the very first turn.)

## pi harness: which models it cannot serve, and why

`_HARNESS_EXCLUDED_MODELS["pi"]` in `omnigent/server/smart_routing.py` names the
(harness, model) pairs corrected AFTER the router verdict. They are deliberately
NOT pruned from the candidate set: the router requires its full scenario menu
and 400s on a partial one, so the full set must be offered and an incompatible
pick moved to a harness that can run it (`_redirect_incompatible_pick`).

The pi harness reaches Databricks two incompatible ways for these families:

- **Claude models** ride pi's Anthropic Messages gateway, whose request path
  adds an `eager_input_streaming` field the serving endpoint rejects with a 400
  when tools are present.
- **The gpt-5.5 / gpt-5.6 reasoning models** ride pi's openai-completions path
  (`/chat/completions`); Databricks applies a default `reasoning_effort` there
  and rejects tool calls with "Function tools with reasoning_effort are not
  supported for gpt-5.5 … use /v1/responses or set reasoning_effort to 'none'."
  pi's provider cannot send that override, so tool turns 400.

Where each excluded pick lands instead: `claude-sdk` serves Claude, and `codex`
serves gpt-5.5+ over the Responses API. The gpt-5.4 family works on pi and is
left alone. `databricks-gpt-5-6-terra` stays on the pi exclusion list (a real
serving incompatibility) even though it is not one of the router's arms.

## Design

Priority order per the goal:

(a) **Running process is truth.** The codex thread's settings are the truth;
`config.toml`'s top-level `model` is the on-disk mirror all omnigent readers
(forwarder mirror, cost-gate hook) already use. Therefore: whenever omnigent
switches the thread model, it must update **both** the thread
(`thread/settings/update`) and the mirror file, the same key an in-TUI
`/model` writes. Last-writer-wins matches user-switch semantics.

(b) **Switch through a surface the harness UI reflects.** The thread-level
switch is already the mechanism, and the TUI's status bar live-updates from
`thread/settings/updated` (probed). The `/model` picker's highlight is
upstream TUI behavior; with the reversion loop fixed the thread genuinely
stays on the routed model, so `/status` / the status bar / a resumed TUI all
agree. The `RoutingDecisionCard` remains the explicit marker of the routed
model in the omnigent transcript.

(c) **Session snapshot/UI shows the live model.** The forwarder's
`external_model_change` mirror already covers harness-observed state; with
the mirror file in sync it reports the routed model instead of clobbering
it. Additionally routing now publishes `session.model` at persist time so
open web clients update immediately; if the harness-side apply fails, the
forwarder's next mirror corrects the value (self-healing, harness wins).

(d) **Launch race closed by the per-turn push.** Because every turn carries
`model_override` → `ExecutorConfig.model` → `thread/settings/update`, a
terminal launched before routing persisted still converges on the routed
model at its first omnigent-driven turn. No launch-ordering change needed.

### Changes

1. `omnigent/codex_native_bridge.py` — new `write_codex_config_model`
   (companion to `read_codex_config_model:277`): upserts the top-level
   `model` key only (stops at the first `[section]`), best-effort
   (`False` on OSError; the live thread already switched).
2. `omnigent/inner/codex_native_executor.py` — in `run_turn`, after a
   successful `thread/settings/update` that carried a `model`, mirror it via
   `write_codex_config_model` (warn on failure). This closes the reversion
   loop and fixes the cost-gate hook's model read for routed turns.
3. `omnigent/server/routes/_sessions/orchestration.py` — new
   `_publish_routed_model` helper; called after routing persists
   `model_override` (top-level path and child path) so the web dropdown
   updates live (same event `_persist_external_model_change` publishes).

### Interaction notes

- In-TUI `/model` still wins: it writes the same `config.toml` key; the
  forwarder mirrors it up; the next turn's `ExecutorConfig.model` equals the
  new `model_override`, so the executor's `thread/settings/update` is a
  no-op re-assert of the user's pick.
- Server-side dedupe (`helpers.py:1940`) prevents event echo loops: the
  forwarder's mirror of an omnigent-initiated switch matches
  `conv.model_override` and no-ops.
- A steered (mid-turn) message skips the settings branch by design; the
  switch lands at the next turn boundary.

### Forwarder hardening (implemented — follow-up packet)

`_CodexForwarderState` now tracks `settings_model` (last live
`thread/settings/updated` model — the running thread's truth) and
`last_config_model` (the config.toml value as of the previous read).
`_refresh_model_from_config` precedence: a config.toml value that CHANGED
since the last read wins (in-TUI `/model` or the executor's mirror write —
freshest signal); an unchanged config defers to `settings_model`; otherwise
the config value is adopted as before. This keeps the routed model even when
the executor's config mirror write fails, while still honoring a genuine
in-TUI `/model` rewrite.

### Launch race — source analysis (no further change needed)

- A terminal created AFTER routing already launches on the routed model:
  `_codex_native_launch_config` GETs the live snapshot at create time and
  prefers `model_override` (`omnigent/runner/native/orchestration.py:694-748`,
  used at `:3458-3473`).
- The racy case is auto-create at session bind (POST /v1/sessions), which by
  definition precedes the first message — `model_override` cannot exist yet,
  so no re-read timing helps, and the pin cannot be skipped (it exists to
  stop the stale shared-config model from being mirrored;
  `codex_native_app_server.py:205-244`, applied at `:635` — a file owned by
  the enforcement agent). The race is closed functionally by the first-turn
  push: the forwarded message carries `model_override` in-band
  (`orchestration.py:3337-3342`), and `CodexNativeExecutor.run_turn` applies
  `thread/settings/update` + the config mirror under `_inject_lock` BEFORE
  `turn/start` — the same locked switch-then-inject discipline as
  claude-native's `/model` injection.

Note: the "native TUI flow" and the executor flow are the same path —
`omnigent/inner/codex_native_harness.py` builds `CodexNativeExecutor` as the
sole injection bridge for web-originated codex-native messages, so the
executor-side push covers the native TUI sessions.

## Implemented (this packet — all files outside the two in-flight agents' sets)

- `omnigent/codex_native_bridge.py`: `write_codex_config_model` (+ `re` import).
- `omnigent/inner/codex_native_executor.py`: mirror write after
  `thread/settings/update`.
- `omnigent/server/routes/_sessions/orchestration.py`:
  `_publish_routed_model` + calls in both routing persist paths.
- Tests: `tests/test_codex_native_bridge.py` (writer upsert/insert/create),
  `tests/inner/test_codex_native_executor.py` (config mirror on model
  switch; effort-only leaves model), and
  `tests/server/integration/test_routing_integration.py`
  (`session.model` SSE published on routed persist).

- `omnigent/codex_native_forwarder.py` (unblocked mid-flight, edits confined
  to the model-mirror regions: state fields, `note_thread_settings_updated`,
  `_refresh_model_from_config`): settings-model preference described above,
  plus regression tests in `tests/test_codex_native_forwarder.py`
  (pushed-model holds over stale config; changed config wins; launch-race
  scenario ends on the routed model).

## Deferred — patch plan for the next packet (owned files in flight)

1. `omnigent/inner/codex_executor.py` (owned): if the SDK-codex harness ever
   gains routing-driven model changes, apply the same "switch + mirror" rule
   there.
2. Upstream/TUI: the `/model` picker's current-selection highlight after a
   remote switch is codex TUI behavior; the status bar already reflects the
   live model on 0.145.0. If a stronger in-terminal marker is wanted, the
   terminal wrapper label (`omnigent/_wrapper_labels.py`) could append the
   live model to the tmux status line, driven by the same `session.model`
   stream — nice-to-have, not required for correctness.
3. Forwarder reconnect edge: a forwarder that reconnects after a failed
   config mirror write re-adopts the stale config at subscription (its
   `settings_model` baseline is per-connection). Acceptable — the next
   routed turn re-pushes and re-mirrors; a full fix would seed
   `settings_model` from the `thread/resume` response, which touches
   resume semantics deliberately left to the forwarder's owner.
