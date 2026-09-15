# Dynamic model + effort selection for ACP harnesses

Status: Implementation plan. Supersedes `dynamic-harness-model-and-effort-proposal.md`
(a Devin user's proposal) by folding in a cross-harness CLI survey, a corrected
ACP capability finding, and a leaner delivery order.

Date: 2026-09-14

## Goal

Let users pick a model **and** reasoning effort for ACP harnesses in (1) the
new-chat composer and (2) mid-session, with the choices **discovered
dynamically** — primarily by asking the harness's own CLI, and by reading what
the ACP session advertises — rather than from static per-harness arrays.

First-class targets: **Devin** and **Grok Build** (both verified installed +
signed in locally). Generic `acp:<slug>`, goose, qwen, jcode inherit the same
path.

## The three things that make this small

1. **The warm switch already exists and already runs before the first prompt.**
   `AcpExecutor.run_turn` reads `config.model` and calls `_apply_model_override`
   → ACP `session/set_config_option {configId:"model"}`, transcript kept
   (`acp_executor.py:1611`, `:1515`). The per-turn `model_override` wire is
   shared by every harness via the adapter (`_executor_adapter.py:182`).
2. **ACP standardizes generic session config options** (`session/set_config_option`,
   `configOptions` in `session/new`, `config_option_update`) — model is just an
   option with `category:"model"`; effort, where an agent exposes it, is
   `category:"thought_level"`; there is also `mode`. Verified against the v1 spec
   and a live Devin probe.
3. **Host-side discovery already has a home.** `HostConnection._handle_model_options`
   (`connect.py:2914`) runs per-harness model probes (codex/claude/pi) on the
   machine where the CLI + vendor login live; it returns "unsupported" for ACP
   today (`connect.py:3013`). Adding an ACP branch is the whole discovery hook.

## Corrected/verified facts that shape the plan

- **No `initialize` capability is needed for select-type config options.** The
  ACP v1 spec gates only *boolean* options behind
  `clientCapabilities.session.configOptions.boolean`; selects (model, effort,
  mode) need no opt-in. Confirmed: omnigent's `initialize` advertises only
  `fs`+`terminal` (`acp_executor.py:714`), yet Devin still volunteers `mode`+`model`
  selects. So the seam is dormant **only** because we discard the choices and
  reject the override for catalog rows — not because of a missing handshake.
- **Devin encodes effort in the model id** (`claude-opus-5-{low,medium,high,xhigh,max}`,
  `gpt-5-6-sol-{none,…,max}`). Its ACP session advertises `mode`+`model` and
  **no** `effort` option (`configId:effort` → `-32002`, per the proposal's probe).
  So for Devin: pick the whole `model_uid`; do **not** send a separate effort
  mutation. Do not parse suffixes to invent a universal effort scale.
- **CLI model-list is a real but non-universal convention.** Verified live/in-repo:
  `devin models list --format json` (rich JSON), `grok models` (bullet text, no
  JSON flag), `cursor-agent models` (in-repo template, `model_catalog.py:1127`).
  No list command: gemini-cli, goose (hosted), opencode, codex. Config-file only:
  qwen (`~/.qwen/settings.json`), jcode (`~/.jcode/config.toml`).
- **The override gate rejects the catalog rows.** `harness_supports_model_override`
  (`model_override.py:285`) is True for `acp`/`goose`/`qwen` but False for
  `devin`/`grok`/`jcode` (they're absent from `model_env_keys()`).
- **The executor keeps too little.** `_note_config_options` (`acp_executor.py:1485`)
  records option ids + the model's `currentValue` only — it drops the `options[]`
  choices. It also uses a process-wide `_model_switch_supported` latch that a
  single rejection turns off for the whole process.

## Architecture

- **Discovery runs on the execution host**, per harness, reusing
  `_handle_model_options` + `/hosts/{host}/harnesses/{harness}/model-options`
  (`hosts.py:684`, helper `_host_model_options.py`). Sources, in preference order:
  (a) the harness's **CLI catalog** (`<cli> models`), (b) the **ACP session's
  advertised `configOptions`** (authoritative once a session exists),
  (c) deferred: a vendor **config file**.
- **Application uses the ACP generic setter** (`session/set_config_option`) keyed
  by the option's `category` — `model` → model, `thought_level` → effort,
  `mode` → mode. The executor already applies `model`; generalize it to any
  advertised select and wire effort from `config.extra["reasoning_effort"]`.
- **One additive config snapshot** flows through the existing host + session
  transport; `models` stays as a compatibility projection. No second cache, no
  parallel discovery per UI surface.
- **Start with direct host branches + executor changes.** Introduce the
  proposal's formal per-harness "configuration provider" interface only when a
  second adapter needs `read`/`apply` behavior the executor doesn't already give
  (YAGNI until then).

### Config-option data contract (from ACP v1)

Each option: `{ id, name, description?, category?, type:"select"|"boolean",
currentValue, options?: [{ value, name, description? }] }`.
`session/set_config_option {sessionId, configId, value}` → response returns the
**complete updated `configOptions` array**. Our normalized snapshot mirrors this
plus `{harness, sessionId?, source, observedAt, revision}`; the UI keys controls
off `category`.

## Delivery phases

### Phase 1 — CLI discovery for Devin + Grok, lit through the existing switch

Smallest end-to-end win. No new UI machinery; reuses the working switch.

- **Catalog rows** (`acp_cli_harnesses.py`): add `models_argv: tuple[str,...]`
  and `models_format: str` to `AcpCliHarness`.
  - devin → `("models","list","--format","json")`, format `devin-json`
  - grok → `("models",)`, format `bullet-text`
- **Host adapter** (`connect.py:_handle_model_options`, new ACP branch before the
  `:3013` else): look up the row; if `models_argv` set, run
  `subprocess.run([binary, *models_argv], timeout≈15, argv array)` and parse:
  - `devin-json`: `families[].variants[].{model_uid→id, label→displayName,
    max_context_tokens→context_window, cost_tier}`
  - `bullet-text`: lines matching `^\s*[*-]\s+(\S+)` (covers grok; cursor's
    existing `parse_cursor_cli_model_options` is the sibling). Graceful `[]` on
    failure → composer falls back to free-text (launch still works: own-auth CLI
    brings its own login, mirroring `model_catalog.py:1097`).
- **Relax the gate**: add the row ids to `model_env_keys()` (all map to
  `HARNESS_ACP_MODEL`, since rows run the generic ACP wrap) so
  `harness_supports_model_override` passes and the per-turn `model_override`
  reaches `_apply_model_override`.
- **Acceptance**: composer + in-session picker list Devin's 254 variants and
  Grok's models on the selected host, including a value newly advertised by the
  CLI without a frontend release; selecting a Devin model confirms it (protocol
  state) **before** the first prompt.
- **Grok switch — verified (2026-09-14 probe).** `grok agent stdio` `session/new`
  (cached_token, no explicit `authenticate`) advertises two selects:
  `model` (category `model`; `grok-4.6`/`grok-4.5`) and `reasoning_effort`
  (category `thought_level`; `{xhigh,high,medium,low}`). `session/set_config_option
  {configId:"model", value:"grok-4.5"}` echoed `grok-4.5`. So Grok is the
  **independent-effort** case (vs Devin's effort-in-id), and both model and effort
  apply through the same generic `session/set_config_option` path Phase 2 builds.
  (`session/delete` is "Method not found" — Grok exposes `session/close` per its
  `sessionCapabilities`; use the advertised teardown.)

### Phase 2 — Generalize the ACP executor (options capture, effort, snapshot)

- **Capture choices**: `_note_config_options` (`acp_executor.py:1485`) records the
  full `options[]` + `category` + `currentValue` per option into a session config
  snapshot, not just ids + active model.
- **Generalize application**: `_apply_model_override` → `_apply_config_options`
  that applies any advertised select by `category`; wire effort from
  `config.extra["reasoning_effort"]` (set by the adapter at
  `_executor_adapter.py:178`) to the `thought_level` option **iff advertised**.
  Non-atomic: apply model first, consume the returned snapshot, then validate +
  apply effort against the refreshed choices; on partial success publish the
  partial effective state and hold a dependent prompt.
- **Emit upward**: add a `ConfigSnapshot` executor event (today the ACP executor
  yields only Text/Reasoning/ToolCall/TurnComplete/Error) so the server + the
  in-session picker reflect live state, including vendor-originated changes.
- **Scoped outcomes**: replace the process-wide `_model_switch_supported` latch —
  invalid value ≠ disable-all; unknown method = disable that mechanism for the
  process/version; timeout = unknown → reconcile before retry/dependent prompt.
- **Idle notifications**: handle `config_option_update` while idle, not only while
  consuming a prompt response.
- **Capability**: no `initialize` change for selects. (Advertise
  `clientCapabilities.session.configOptions.boolean` only if/when we add boolean
  options — deferred.)
- **Acceptance**: model switch + (where advertised) effort apply mid-session with
  transcript kept; the picker reflects the live snapshot; a rejected effort leaves
  a visible partial state and blocks no dependent prompt.

### Phase 3 — Dynamic effort in shared UI

- **Controls** (`web/src/components/HarnessConfigControls.tsx:135`): replace the
  static Claude/Pi effort arrays with runtime option data from the snapshot;
  refresh effort choices when the model changes; clear an incompatible pending
  effort with a visible explanation before submit.
- **Composer** (`NewChatDialog.tsx`): un-gate model/effort submission from
  native-only branches; drive it off the `acpHarness` flag
  (`useAvailableAgents.ts:467`) + the snapshot. Reuse the same controls in the
  in-session settings card and scheduled tasks.
- **Server**: extend the model-options frame + session snapshot/events additively
  with the config snapshot (keep `models` as a compat projection); add a
  session-config mutation endpoint alongside the existing session model API, same
  auth + runner routing. Unify Omnigent-owned `/model` + `/effort` on it.
- **Acceptance**: choosing model+effort together applies model-first then
  effort-against-refreshed-choices for a harness that exposes independent effort;
  Devin renders grouped, fully-labeled variants with **no** separate effort picker.

### Phase 4 — Deferred / breadth (build when needed)

- **Prepare-session** for pure-ACP agents with no CLI list (jcode/qwen): create
  the real session without prompting, read its options, reuse it on send, tear
  down abandoned prepares. Riskiest surface (session leaks) — Devin/Grok/cursor
  don't need it.
- **Config-file discovery** strategy (qwen `settings.json`, jcode `config.toml`).
- **Formal provider contract** (`discover`/`read_configuration`/`apply_configuration`
  on `HarnessContribution`) wrapping Codex/Claude/Pi + SDK adapters — a refactor
  with no behavior change; do it once ≥2 adapters justify the interface.
- Concurrent-client revision conflicts + per-session mutation queue; resume/fork
  inheritance; boolean options; mode controls (keep permission-policy enforcement).

## Testing

- **Unit**: parser fixtures (`devin-json`, `bullet-text`); `category` → control
  mapping; model-before-effort ordering; option-removal on snapshot replace;
  scoped-failure outcomes; effort dropped when `thought_level` absent; gate now
  passes for devin/grok rows.
- **Live e2e** (host with the CLI logged in): run the three inspection commands;
  select a non-default Devin variant → confirm it's the model at the first prompt
  via protocol state (not by asking the model); mid-session switch keeps
  transcript; Grok discovery lists models; rejected change surfaces + blocks no
  dependent prompt.

## Out of scope / non-goals

- No new catalog service, no harness-architecture rewrite (additive only).
- No suffix-parsing to synthesize a cross-vendor effort scale.
- Selectable models ≠ smart-routing eligibility: a vendor's adaptive model is a
  vendor choice, not an implicit request to enable Omnigent smart routing.
- Vendor CLI model ids keep their exact spelling through selection and dispatch;
  gateway normalization runs only for a gateway model namespace.

## Anchors (current HEAD)

- `omnigent/inner/acp_executor.py`: `_ensure_initialized:714`, `_ensure_session:803`,
  `_note_config_options:1485`, `_apply_model_override:1515`, `run_turn:1576`,
  `requested_model = config.model:1611`
- `omnigent/host/connect.py`: `_handle_model_options:2914`, unsupported-else`:3013`
- `omnigent/models/model_override.py`: `harness_supports_model_override:285`,
  `_SDK_MODEL_OVERRIDE_HARNESSES = frozenset(model_env_keys()):44`
- `omnigent/runtime/harnesses/_executor_adapter.py`: `reasoning_effort:178`,
  `ExecutorConfig(...):182`
- `omnigent/util/reasoning_effort.py`: `efforts_for_harness:106`
- `omnigent/acp_cli_harnesses.py` (`AcpCliHarness`), `omnigent/harness_capabilities.py`,
  `omnigent/harness_plugins.py` (`model_env_keys`), `omnigent/models/model_catalog.py:1127`
  (`_fetch_cursor_cli_listing` template), `web/src/components/HarnessConfigControls.tsx:135`
- ACP spec: https://agentclientprotocol.com/protocol/v1/session-config-options
