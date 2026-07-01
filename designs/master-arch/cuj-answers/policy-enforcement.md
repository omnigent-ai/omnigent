# CUJ Answers — Policy Enforcement, Approvals & Elicitations

> Domain: policies / approvals / elicitations. Verifies & deepens
> `designs/CUJ-ANALYSIS.md §2.D` (which was already accurate — line numbers
> confirmed/updated below). Code is ground truth; trace
> `cfb59197f6f92755270a4d785d3ee3e1` (in `conv_63542a5f92e24956812e19b104eac0e9`)
> supplies live span evidence.

---

## Q1. How are policies CREATED?

There are **three sources** of policies, merged at engine-build time
(`builder.py:309`, run order `session + agent + admin` + a hardcoded gate):

### (a) Session-level (agent-/user-added at runtime)
- Agent calls **`sys_add_policy`** (`tools/builtins/policy.py:18`) after browsing
  **`sys_policy_registry`** (`:94`). The runner forwards to
  **`POST /v1/sessions/{id}/policies`** (`session_policies.py:148`).
- Validation: for `type:python`, the handler **must be in the registry allowlist**
  (`is_registered_handler`, session_policies.py:181 → registry.py:156) — this is
  the anti-RCE guard (an arbitrary dotted path would be imported & called).
  `factory_params` validated against the registry schema (`validate_factory_params`).
- Persisted in the `policies` table with the owning `session_id`; **activates
  immediately** (the next per-call engine build loads it via
  `_load_session_policy_specs`, builder.py:292).
- Failure: duplicate name → 409 (`IntegrityError`), bad params → 400.
- **Hardcoded guard:** `sys_add_policy` itself is gated — `_ASK_ON_ADD_POLICY_SPEC`
  (`builder.py:64`, handler `safety.ask_on_add_policy`) is **unconditionally
  appended to every engine** (`builder.py:315`), so an agent can never add a
  policy without a human ASK.

### (b) Server / admin default
- **`POST /v1/policies`** (`default_policies.py:129`), gated by **`_require_admin`**
  (`:70` — 401 if unauth'd, 403 if not admin, in multi-user mode).
- Stored in the same `policies` table with **`session_id IS NULL`**; applies
  **server-wide to all sessions** (loaded via `RuntimeCaps.default_policies`,
  appended last in run order → "admin gets the last word").
- Same registry allowlist applies (admins are NOT exempt — custom handlers must
  be added via the server `policy_modules` config).

### (c) Spec-declared (immutable)
- Agent YAML `guardrails.policies:` block → `source="spec"`. Surfaced in the LIST
  endpoint with **`id=None`** and **cannot be PATCHed or DELETEd**
  (session_policies.py LIST `:236`, `_spec_to_response:72`).

### Update / enable-disable / remove
- `PATCH /v1/sessions/{id}/policies/{pid}` (`:275`) and `PATCH /v1/policies/{pid}`
  (`:234`): mutate `name`, `handler`, `enabled` (the disable toggle). `type` is
  **immutable** (delete + recreate to change). PATCH re-checks the registry
  allowlist for handler changes (no back door, `:323`).
- `DELETE …/{pid}` (idempotent, returns `{deleted:true}`). LEVEL_EDIT (session)
  / admin (default) required.

---

## Q2. Enforcement: SERVER-level vs SESSION/RUNNER-level

There is **one engine** (`PolicyEngine`, `runtime/policies/engine.py:43`) reached
from **two surfaces**:

### SERVER-level (engine runs inside omni-server) — the default + spec + admin path
Three entry points, all building the engine via `_build_policy_engine_from_spec`
(sessions.py:10352 → `build_policy_engine`):
1. **REQUEST / RESPONSE gates** — `POST /events` → `_evaluate_input_policy`
   (sessions.py:10801) / `_evaluate_output_policy` (:10971). Trace: `policy.evaluate
   phase=REQUEST` is a child of `POST /events`.
2. **TOOL_CALL + TOOL_RESULT (server MCP proxy)** — `POST /mcp` `tools/call` →
   `_handle_mcp_tools_call` (:13135). This is how `sys_*` and spec-MCP tools are
   gated for SDK harnesses. Trace: `policy.evaluate phase=TOOL_CALL` and
   `phase=TOOL_RESULT` are children of `POST /mcp`.
3. **The generic hook path** — `POST /policies/evaluate` → `evaluate_policy`
   (:15973). Used by native PreToolUse/UserPromptSubmit hooks, by the runner
   proxying LLM-phase events, and by SDK connector-native tools. Trace:
   `policy.evaluate phase=LLM_REQUEST` and `phase=LLM_RESPONSE` are children of
   `POST /policies/evaluate`. The route also owns **LLM-phase gating** and the
   **elicitation registry** (it collapses ASK→ALLOW/DENY via `_hold_native_ask_gate`).

### RUNNER-level fast-path (engine runs on the runner) — `RunnerToolPolicyGate`
- `runner/policy.py:109`. Runs **only function-type policies whose `on:` includes
  TOOL_CALL/TOOL_RESULT** (label/prompt types stay server-side — they need the
  ConversationStore / LLM classifier the runner lacks, `runner/policy.py:14-18`).
- **ALLOW/DENY decided locally before MCP dispatch** (no server round-trip). DENY
  feeds `[Denied by policy: …]` back as the tool output (`format_deny_text:314`).
- **ASK escalates to the server**: the gate surfaces ASK to its caller
  (`runner/tool_dispatch.execute_tool`), which POSTs `evaluate_policy=True` /
  `POST /policies/evaluate`; the server independently re-evaluates and **owns the
  elicitation channel**; the runner awaits via `pending_approvals`. Dual
  evaluation is intentional (`runner/policy.py:20-34`).
- `evaluate_tool_result` collapses **ASK→DENY** on the result phase (output
  already exists, no clean rollback, `runner/policy.py:184-226`).

**Note (trace reality):** the observed claude-sdk run routed TOOL_CALL/TOOL_RESULT
through the **server `/mcp` proxy**, not the runner fast-path — because `sys_os_shell`
is an Omnigent tool dispatched server-side. The runner fast-path fires for
*function-type spec policies* bound to tools, and for connector-native tools.

---

## Q3. Phases, composition order, fail-closed vs fail-open

### Phases (5)
`Phase.REQUEST` (input gate, pre-LLM) · `Phase.TOOL_CALL` (the main gate) ·
`Phase.TOOL_RESULT` (post-execution, can only DENY/redact) · advisory
`Phase.LLM_REQUEST` / `Phase.LLM_RESPONSE`. Proto map at sessions.py:15946.
**All five observed in the trace** (see architecture §5 table).

### Composition order (`_evaluate_composed`, engine.py:284)
- Run order: **session policies → agent-spec policies → admin defaults**, then
  `__ask_on_add_policy` appended (builder.py:309-315). Sub-agents **inherit root
  session policies prepended** (builder.py:301-307, child wins on name collision).
- **First DENY short-circuits** (engine.py:370 → `_compose_deny`). ASK accumulates
  but the loop continues (a later DENY overrides). ALLOW continues; `data` chains
  forward (`ctx.content` replaced, engine.py:378-382) so policies transform
  sequentially.
- Each policy gated by `_should_fire` (engine.py:457): **PhaseSelector match**
  (cheap) then **`condition:` label-gate** (`_condition_matches:1237` — AND across
  keys, list = OR within key).

### Fail-CLOSED vs fail-OPEN
- **`FAIL_CLOSED_PHASES = ("PHASE_TOOL_CALL", "PHASE_REQUEST")`** (`types.py:61`),
  the single source of truth. Used by `_evaluate_policy_via_omnigent`
  (runner/app.py:6297) and the native hooks (native_policy_hook.py:60-67): on a
  server outage / non-2xx / unparseable 200 these phases → **DENY**.
- **TOOL_RESULT, LLM_REQUEST, LLM_RESPONSE fail OPEN** → ALLOW (advisory, or
  side-effect already incurred — denying a post-execution result only blocks an
  already-done action).
- **Per-policy fail-closed** (`_fail_closed`, engine.py:1038): a raising/invalid
  policy → DENY unless its declared `action` list is classifier-only (`[allow]`→
  ALLOW) or ask-gate (`[ask]`/`[allow,ask]`→ASK).

---

## Q4. The ASK flow end-to-end (approve / deny / timeout)

1. Engine composes **ASK**, returns it with **withheld** `set_labels` +
   `state_updates` + `deciding_policies` (engine.py:389-402; POLICIES.md §7.2:
   "a denied ASK leaves no trace").
2. The enforcement site parks. **Two parking models:**
   - **Server-parked** (REQUEST gate, native TOOL_CALL via hook, LLM_REQUEST):
     `_hold_native_ask_gate` (sessions.py:4119) registers a Future in
     `_harness_elicitation_registry`, publishes **`response.elicitation_request`**
     (mode `url` → links to `/approve/{sid}/{eid}`), and blocks on
     `_publish_and_wait_for_harness_elicitation` (:1397).
   - **Runner-parked** (SDK relay TOOL_CALL): `_evaluate_tool_call_policy`
     (sessions.py:10556) returns `verdict:"pending"` + `elicitation_id` +
     `ask_timeout`; stashes deferred writes in `_pending_policy_ask_writes` (:10649);
     the runner parks on its `_pending_approvals` Future.
3. **Web ApprovalCard** renders from the SSE event; the sidebar badge increments
   because **`pending_elicitations.record_publish`** auto-tracks every
   `response.elicitation_request` flowing through `session_stream.publish`
   (`pending_elicitations.py:81`). The standalone `/approve` page reads the prompt
   via `GET /elicitations/{eid}` (sessions.py:18089 → `pending_elicitations.lookup`).
4. **Resolve endpoint** — `POST /v1/sessions/{id}/elicitations/{eid}/resolve`
   (sessions.py:18022) with `ElicitationResult {action}`. (Equivalent: the legacy
   `type:"approval"` event on `POST /events`.) Both funnel into
   **`_resolve_elicitation`** (sessions.py:3921) which does 3 things in order:
   (a) **set the server-side Future** (owner-checked — only the owning session may
   resolve, :3983); (b) publish **`response.elicitation_resolved`** (badge −1,
   ApprovalCard flips, idempotent); (c) **forward to runner** as a canonical
   `approval` event (`_forward_approval_to_runner:3880`) so runner-parked
   Futures resolve.
5. **Future resolves** → the parked gate wakes. **APPROVE** (`action=="accept"`,
   strict — `_parse_verdict:290`): apply the withheld `set_labels` +
   `state_updates` (`_hold_native_ask_gate:4212`, or `_await_elicitation:146`, or
   `_apply_pending_policy_ask_writes:10372` for the relay path). **DENY / cancel /
   malformed / timeout / disconnect**: discard withheld writes, verdict → DENY.
6. Verdict reaches the action: native hook returns
   `permissionDecision: allow|deny`; SDK tool proceeds or sees `[Denied by policy: …]`.

`ask_timeout`: per-policy override wins over spec default (`resolve_ask_timeout:264`);
the relay path returns it so the runner's park honors the same cap; native
PermissionRequest caps at one day (`_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S=86400`).

---

## Q5. Which HOOKS each in-scope harness must expose, and HOW verdicts return

**Required-hooks contract** = the hooks that must reach `/policies/evaluate` (or
the permission webhook) for *all* policies to be enforced on that harness:

| Harness | Required hooks | How verdicts return |
|---|---|---|
| **claude-native** | **PreToolUse** (→ `/policies/evaluate` PHASE_TOOL_CALL) + **UserPromptSubmit** (→ PHASE_REQUEST) + **PermissionRequest** (→ `/hooks/permission-request`) | **long-poll HTTP** — verdict in the held response body (`PermissionRequest` → `hookSpecificOutput.decision.behavior: allow\|deny`, sessions.py:15638/15801; empty 200 = "defer to TUI"). For *policy* ASK the server holds the `/policies/evaluate` long-poll (`_hold_native_ask_gate`) and collapses ASK→hard ALLOW/DENY so a permissive `permission_mode` can't auto-approve (sessions.py:16113). PostToolUse is observational (PHASE_TOOL_RESULT, fail-open). |
| **codex-native** | codex `PreToolUse`/`UserPromptSubmit`-equivalents (shared `native_policy_hook`) + **`codex-elicitation-request`** hook | long-poll HTTP via the codex elicitation hook (`codex_elicitation_request_hook`, sessions.py:16214; `codex_elicitation_id`). Codex hook stamps live `/model` into `event.context.model`. **No live trace (creds 403).** |
| **claude-sdk / codex (SDK) / polly** | server `type=approval` event (no command hooks; the SDK loop calls the server in-process) | runner **`pending_approvals` Future** for relay TOOL_CALL ASK; the SDK `can_use_tool` callback (`claude_sdk_executor.py:1680`) also runs an elicitation handler for connector-native tools. **No keystroke emulation.** |

**Single shared translation layer**: `native_policy_hook.py` converts the native
hook shape ↔ proto `EvaluationRequest`/`EvaluationResponse` for both claude & codex
(output differs by event: PreToolUse → `permissionDecision`; UserPromptSubmit →
top-level `decision/reason`). It carries a baked one-shot auth token
(`policy_hook_wrapper_script:103`) — **this token expiring is the §2.G bug that
fails every native tool call closed.**

> The verdict for the in-scope harnesses returns via **long-poll HTTP**
> (claude-native / codex-native) or an **`approval` event → Future** (SDK family).
> No emulated keystrokes for any in-scope harness (some out-of-scope native
> harnesses use tmux keystroke delivery; not relevant here).

---

## Q6. Read-only eval, label gating, pending-elicitation tracking + sidebar badge

### Read-only eval (LEVEL_READ)
- `evaluate_policy` route computes `is_read_only = level < LEVEL_EDIT`
  (sessions.py:16005) and passes `read_only=True` to `engine.evaluate`.
- The engine **skips all persistence** — no label writes, no state updates, on
  both ALLOW and DENY paths (engine.py:403, 446) — but still returns `set_labels`/
  `state_updates` so a caller can audit "what *would* have changed."
- Read-only callers **never enter the ASK gate** (sessions.py:16130) — parking
  mints an elicitation = a mutation; they get the raw ASK verdict instead.
- Use case: LEVEL_READ collaborators auditing "what would be denied" without
  side effects.

### Label gating (`condition:`)
- A policy fires only when its `condition:` block matches the conversation's label
  hot-cache (`_should_fire:457` → `_condition_matches:1237`). AND across keys; a
  list value is OR within the key; a missing label never matches (gate stays closed).
- Labels are written *through* the engine: `apply_label_writes` (engine.py:490)
  validates against `LabelDef` (`values` enum + `monotonic` direction —
  `_filter_schema_valid:820`, `_monotonic_ok:1190`) and UPSERTs to
  `conversation_labels` + the hot cache. Unschema'd labels set freely.
- Monotonic merge during one evaluate keeps the most-restrictive value across
  policies writing the same key (`_merge_monotonic_writes:1117`).

### Pending-elicitation tracking + sidebar badge
- In-process index `runtime/pending_elicitations.py` — populated automatically by
  `record_publish` (`:81`) on every `response.elicitation_request` passing through
  the single `session_stream.publish` chokepoint (server policy ASKs, claude
  PermissionRequest, runner-relayed elicitations all funnel through it).
- `count_for`/`counts_for` back the **sidebar badge** (`GET /v1/sessions`
  `pending_elicitations_count`). `snapshot_for` **replays** outstanding prompts
  into `GET /v1/sessions/{id}` on **cold load** (the SSE stream has no replay).
- Decremented by `resolve` (approval-dispatch path) or by a
  `response.elicitation_resolved` event flowing through `record_publish`.
- Lifecycle is **tied to the parked awaiter** — when omni-server dies, both the
  index and the Future die together, so the badge can't show phantom pending rows.
- **Caveat:** in-process only → a multi-replica deploy splits the badge per replica
  (documented; needs a backplane).

---

## Adjacent: builtin catalog (the policies users actually attach)

From `BUILTIN_POLICY_MODULES` (`builtins/__init__.py:37`) + `POLICY_REGISTRY` scan:
- **cost** (`cost.py`): `cost_budget`, `user_daily_cost_budget`, `subagent_cost_budget`
  — the canonical **ASK + `state_updates`** soft-checkpoint pattern (ASK at each
  `ask_thresholds_usd`, remembered per-session/user-day/subtree; DENY at
  `max_cost_usd` while on an expensive model → prompts a `/model` downgrade).
- **safety** (`safety.py`): `max_tool_calls_per_session`, `ask_on_os_tools`,
  `block_skills`, `enforce_sandbox`, `deny_pii_in_llm_request`, `ask_on_add_policy`.
- **prompt** (`prompt.py`): `prompt_policy` = the **LLM-classifier** (`PromptPolicy`,
  uses the injected `PolicyLLMClient`).
- **risk_score** (`risk_score.py`): `risk_score_policy`.
- **routing** (`routing.py`): `deny_trivial_to_expensive_model` (model-cost routing).
- **cel** (`cel.py`): `cel_policy` (declarative CEL expression gate).
- **google/github/working_dir**: connector + working-dir guards.

---

## Verification of CUJ-ANALYSIS §2.D (line-number confirmation)

§2.D was **accurate**. Confirmed/updated anchors:
- `sys_add_policy` → `POST …/policies` → `session_policies.py:148` ✓ (doc said `:148`).
- admin default → `default_policies.py:129`, `_require_admin` ✓.
- The ASK flow / resolve endpoint: doc cited `:17611` for resolve — the actual
  `resolve_elicitation` route is **`sessions.py:18022`** and the shared resolver is
  **`_resolve_elicitation` :3921** (line drift; mechanism identical).
- Required-hooks table (claude-native PreToolUse+PermissionRequest long-poll;
  codex-native elicitation hook; SDK approval-event) ✓ — all confirmed in code.
- Read-only eval, label gating, pending-elicitation tracking ✓.
- One correction of emphasis: the doc's table omits **UserPromptSubmit** as a
  required claude-native hook — it IS required (it is the *sole* REQUEST-phase gate
  for native sessions; the server `/events` input gate is deduped for native via
  `pending_inputs`, sessions.py:16065).
