> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Policy & Elicitations

> Guardrails, the in-process policy engine, approval (ASK) gates, and the per-harness
> hooks that make policies enforceable. All `path:line` below were opened & confirmed in
> the `master-arch-docs` worktree (main + telemetry PR #1617). `(unverified)` tags claims
> I could not pin to code.

## 1. Role & boundaries

The policy subsystem decides **ALLOW / ASK / DENY** at five enforcement phases and resolves
human approvals (elicitations). It owns:
- The **in-process engine** `PolicyEngine` (`omnigent/runtime/policies/engine.py:43`) — the single
  choke point; every eval routes through `evaluate()` which wraps a `policy.evaluate` telemetry span.
- **Policy resolution / composition** (DENY short-circuit, ASK accumulation, label/state side-effects).
- The **ASK / elicitation flow** (server-side held gate + in-process workflow gate), the web
  `ApprovalCard` wire contract, and the resolve→Future→forward plumbing.
- The **per-harness hooks** that surface tool calls / prompts to the engine, and the
  **server held-response (long-poll)** that returns an ASK verdict to a native hook.
- The **handler allowlist** (RCE guard) for user-attached policies.

It does **NOT** own: the harness's own consent gate (Claude `PermissionRequest` web routing is a
*separate* gate — see §6), tmux/keystroke delivery (only out-of-scope native harnesses use that),
sandboxing (OmniBox; policies are a safety net, not a security boundary — `nessie/policies.py:142`),
or credential minting (it consumes the auth-header factory, §5).

## 2. Key files & entrypoints (verified)

| File:line | What |
|---|---|
| `omnigent/runtime/policies/engine.py:43` | `PolicyEngine`; `evaluate()` @ `:230` (span wrap), `_evaluate_composed()` @ `:284` (DENY short-circuit + ASK accumulate) |
| `engine.py:1038` | `_fail_closed()` — the 3-way fail policy (ALLOW/ASK/DENY by declared action list) |
| `engine.py:976` | `_dispatch_policy()` — wraps every `policy.evaluate` in try/except → `_fail_closed` |
| `omnigent/runtime/policies/approval.py:80` | `_await_elicitation()` — in-process workflow ASK gate (registers `__elicitation__` row, parks on `tool_result` topic) |
| `approval.py:175` | `build_elicitation_request_event()` — the `response.elicitation_request` wire shape (MCP `ElicitRequestFormParams`); URL mode → `/approve/{sid}/{eid}` |
| `approval.py:290` | `_parse_verdict()` — strict: only `action=="accept"` → True (fail-closed) |
| `omnigent/runtime/policies/enforcement.py:21` | `_enforce_policy()` — thin call-site wrapper used by the 4 workflow sites |
| `omnigent/runner/policy.py:109` | `RunnerToolPolicyGate` — runner MCP fast-path (function-type TOOL_CALL/TOOL_RESULT only) |
| `omnigent/native_policy_hook.py` | **shared** claude+codex hook conversion: hook payload→`EvaluationRequest`, `EvaluationResponse`→hook output, `post_evaluate_with_retry`, `fail_closed_hook_output`, `policy_hook_reauth` |
| `omnigent/claude_native_hook.py:73` | claude policy hook `main()`; `permission-request` subcmd @ `:84` (separate gate); `_PERMISSION_TIMEOUT_S=86400` @ `:46` |
| `omnigent/codex_native_hook.py:43` | codex policy hook `main()` (same `PreToolUse/PostToolUse/UserPromptSubmit` shape) |
| `omnigent/codex_native_forwarder.py:3007` | `_handle_codex_elicitation_request` — codex's *own* permission prompt (`item/tool/requestUserInput`) → `/hooks/codex-elicitation-request` |
| `omnigent/codex_native_elicitation.py:24` | `codex_elicitation_id()` — deterministic id so `serverRequest/resolved` clears the right card |
| `omnigent/server/routes/session_policies.py:148` | `POST /v1/sessions/{id}/policies` (session create); registry allowlist guard @ `:181` |
| `omnigent/server/routes/default_policies.py:129` | `POST /v1/policies` (admin default); `_require_admin` @ `:70` |
| `omnigent/server/routes/sessions.py:15964` | `POST /sessions/{id}/policies/evaluate` (the native-hook + SDK-relay eval endpoint) |
| `sessions.py:4119` | `_hold_native_ask_gate()` — server-side held ASK gate (TOOL_CALL/LLM_REQUEST/REQUEST) |
| `sessions.py:1397` | `_publish_and_wait_for_harness_elicitation()` — parks the server-side Future, 3-way race |
| `sessions.py:18014` | `POST /sessions/{id}/elicitations/{eid}/resolve` route |
| `sessions.py:3978` | approval dispatch: `_harness_elicitation_registry[eid].set_result(...)` (the Future resolution) |
| `sessions.py:10801` | `_evaluate_input_policy()` — REQUEST gate at `POST /events` (SDK/web path; bypassed for native) |
| `sessions.py:10556` | `_evaluate_tool_call_policy()` (server-side relay tool gate) |
| `omnigent/runner/app.py:6248` | `_evaluate_policy_via_omnigent()` — SDK relay: `policy_evaluation.requested` SSE → POST evaluate → `policy_verdict` event |
| `omnigent/runtime/pending_elicitations.py` | in-memory index of outstanding elicitations (sidebar badge + cold-load replay) |
| `omnigent/policies/registry.py:156` | `is_registered_handler()` — the RCE allowlist |
| `omnigent/inner/nessie/policies.py:346` | `blast_radius`, `spawn_bounds` (`:408`), `worktree_guard` (`:520`), `read_only_os` (`:572`) — FunctionPolicy factories, run runner-side |
| `omnigent/policies/builtins/` | `safety.py`, `cost.py`, `risk_score.py`, `routing.py` (classifier `deny_trivial_to_expensive_model`), `prompt.py`, `github.py`, `google.py`; modules scanned via `BUILTIN_POLICY_MODULES` (`builtins/__init__.py:37`) |
| `omnigent/tools/builtins/policy.py:33` | `sys_add_policy` MCP tool → `POST /v1/sessions/{id}/policies`; `sys_policy_registry` reads `/v1/policy-registry` |

## 3. Internal model

**`PolicyEngine`** — per-workflow, plain object built at top of `_run_agent_loop` (no ContextVar).
Holds `policies: list[Policy]` in **YAML declaration order**, a hot **label cache** (`_labels`,
write-through to `conversation_labels`), `_session_state` (write-through to `session_usage`/state),
cumulative `_usage` + optional `_subtree_usage` / `_user_daily_cost`, resolved `_model`, and an
optional `_llm_client`. State is snapshotted **at construction** — a fresh `build_policy_engine`
is the only way to see a sibling's just-recorded approval (`sessions.py:16087` `_build_engine`).

**`Policy` kinds** (`omnigent/policies/`): `FunctionPolicy` (dotted-path callable, evaluated runner- or
server-side), `PromptPolicy` (LLM classifier), `LabelPolicy`. Spec carries `on:` (PhaseSelector list),
`condition:` (label gate), `action:` whitelist, `set_labels:` whitelist, `ask_timeout` override.

**`Phase` enum** (`spec/types.py:1074`): `REQUEST · TOOL_CALL · TOOL_RESULT · LLM_REQUEST · LLM_RESPONSE`
(values `request/tool_call/tool_result/llm_request/llm_response`). Tool-name narrowing only valid on
TOOL_CALL/TOOL_RESULT (`spec/types.py:1189`). Proto map `_PROTO_EVENT_TYPE_TO_PHASE` @ `sessions.py:15946`.

**`PolicyResult`** = `action` + `reason` + `set_labels` + `state_updates` + `deciding_policies` + `data`
(content-rewrite payload, e.g. PII redaction). DENY carries one `deciding_policy`; ASK carries the full
`deciding_policies` list (reasons joined `"; "`).

## 4. Composition & fail-open/closed

**Composition** (`engine.py:284` `_evaluate_composed`): iterate policies in YAML order; per policy:
skip if `PhaseSelector` no-match (`_should_fire` @ `:457`) or `condition:` label-gate no-match
(`_condition_matches` @ `:1237`, AND across keys / OR within a list); else dispatch. Then:
1. **DENY → short-circuit immediately** (`_compose_deny` @ `:414`) — applies accumulated writes from
   ALLOWing predecessors + the DENYer's own, returns DENY. No later policy can override.
2. **ASK accumulates** (does not short-circuit) — a later policy may still DENY. After the loop, if any
   ASK: return ASK carrying **withheld** `set_labels`/`state_updates` (applied only on approve, §7.2).
3. Else ALLOW (apply accumulated writes).
4. **Data chaining**: a policy returning `data` feeds it forward as the next policy's `ctx.content`
   (`engine.py:382`) — sequential transform (e.g. redact then classify).
- Monotonic label merge across one eval: `_merge_monotonic_writes` @ `:1117` keeps the most-restrictive
  value per the LabelDef direction (a later policy can't lower an `increasing` taint label).

**Fail policy** — two layers, both phase-aware:

| Layer | Mechanism | TOOL_CALL | REQUEST | TOOL_RESULT | LLM_REQUEST | LLM_RESPONSE |
|---|---|---|---|---|---|---|
| **Per-policy exception** (`_fail_closed` engine.py:1038) | depends on declared `action:` list, NOT phase | DENY (default) / ALLOW if `[allow]` classifier-only / ASK if `[ask]`/`[allow,ask]` | same | same | same | same |
| **Eval unreachable** (native hook `fail_closed_hook_output`; runner relay `_evaluate_policy_via_omnigent`) | phase-aware, keyed on `FAIL_CLOSED_PHASES=("PHASE_TOOL_CALL","PHASE_REQUEST")` (`policies/types.py:61`) | **CLOSED → deny** | **CLOSED → block** | **OPEN → None** | **OPEN → allow** | **OPEN → allow** |

Key nuance the CUJ table flattens: **a broken/raising policy** is *not* purely fail-closed — a
classifier-only `[allow]` policy substitutes ALLOW (honours "never blocks"), an `[ask]`/`[allow,ask]`
gate substitutes ASK; only DENY-capable / no-list policies fail-closed DENY (`engine.py:1062-1089`).
"Fail-CLOSED on TOOL_CALL/REQUEST" applies to the **transport-unreachable** path (`FAIL_CLOSED_PHASES`),
not to a policy that throws.

## 5. Inter-component channels (in/out)

```
                          (TUI types prompt)          (web prompt — already gated)
                                  │                              │
 [harness]                       UserPromptSubmit hook       POST /events (_evaluate_input_policy)
   PreToolUse/PostToolUse ───────────┐                           │  REQUEST gate (SDK/web only)
   (native, claude+codex)            ▼                           ▼
                    POST /v1/sessions/{id}/policies/evaluate  ─────────────► [omni-server] PolicyEngine.evaluate
 [harness SDK]   policy_evaluation.requested (SSE)                                  │ policy.evaluate span
   ──► [runner] _evaluate_policy_via_omnigent ──POST evaluate──┘                    │
   ◄── policy_verdict (inbound event)                                               │ ASK?
 [runner MCP]   RunnerToolPolicyGate (fast-path ALLOW/DENY) ── ASK → POST evaluate ─┘
                                                                                    ▼
                                                            _hold_native_ask_gate / _await_elicitation
                                                                                    │
                                            publish response.elicitation_request (SSE) ──► web ApprovalCard / REPL
                                            parks server-side Future / tool_result topic
   web APPROVE ── POST /elicitations/{eid}/resolve ──► dispatch (sessions.py:3978) set_result(Future)
                                            also publishes response.elicitation_resolved + forwards approval→runner
```

- **harness ⇄ server**: REST `POST /policies/evaluate` (native hooks; the SDK relay). The ASK verdict
  is returned **in the held HTTP response body** (long-poll): the POST blocks until a human resolves
  (`read_timeout` ≈ 1 day, `_EVALUATE_POLICY_TIMEOUT_S=86400`), then the response is a *hard* ALLOW/DENY —
  the hook **never sees ASK** (`sessions.py:16125-16174`). **Trace evidence** (`conv_eb24…`, policy-guard):
  edge `omni-runner → omni-server [POST /v1/sessions/{id}/policies/evaluate] x1`, two `policy.evaluate`
  spans on `omni-server` capturing REQUEST content (`"Use the shell to run exactly: echo hello…"`) and
  LLM_REQUEST content (`{"messages_count":1,"tools_count":15,"system_prompt_preview":…}`). In `conv_32db…`
  (sdk-tools) a third span captures LLM_RESPONSE (`text_preview` + `usage`).
- **runner ⇄ server (SDK relay)**: harness emits `policy_evaluation.requested` SSE → runner POSTs evaluate
  → verdict back to harness as `policy_verdict`. Same endpoint, different caller.
- **server → clients**: SSE `response.elicitation_request` / `response.elicitation_resolved`.
- **client → server**: REST `POST /elicitations/{eid}/resolve` (or a session `type=approval` event on
  `POST /events`) carrying MCP `ElicitationResult` `{action, content?}`.
- **server → runner**: approval forwarded as canonical `approval` event (`_forward_approval_to_runner`
  `sessions.py:3880`) so a runner-parked `pending_approvals` Future resolves.
- **hook ⇄ server auth**: hook wrapper bakes one-shot bearer (`policy_hook_wrapper_script`
  `native_policy_hook.py:103`); on 401/302-to-`/oidc/` it re-mints once via `policy_hook_reauth` (`:133`).

## 6. Required hooks per harness (the "make ALL policies work" question)

A harness must expose enough hooks that the engine sees **every** policy-relevant event (request,
tool call, tool result) *and* can deliver an ASK verdict. The Omnigent **policy** gate and the user's
**own consent** gate are deliberately separate (`native_policy_hook.py:285-315`): the policy hook returns
"no opinion" (`None`) on ALLOW so the harness's native permission prompt — and, for Claude, the
`PermissionRequest`→web routing — still fires.

| Harness | Hooks it MUST expose for all policies | REQUEST gate | TOOL_CALL gate | TOOL_RESULT | ASK verdict delivery |
|---|---|---|---|---|---|
| **claude-native** | (1) `UserPromptSubmit`+`PreToolUse`+`PostToolUse` → `/policies/evaluate` (policy gate); (2) `PermissionRequest` → `/hooks/permission-request` (separate **consent** gate, day-long long-poll) | `UserPromptSubmit` (sole REQUEST gate; server `_evaluate_input_policy` bypassed for native) | `PreToolUse` | `PostToolUse` (warn-only) | **long-poll HTTP held response** (verdict in `/policies/evaluate` body) |
| **codex-native** | (1) `PreToolUse`/`PostToolUse`/`UserPromptSubmit` → `/policies/evaluate` (same shared hook code); (2) forwarder relays codex's own `item/tool/requestUserInput` → `/hooks/codex-elicitation-request` | `UserPromptSubmit` | `PreToolUse` | `PostToolUse` | **long-poll HTTP held response** (policy ASK); codex's own permission prompt resolves via `serverRequest/resolved` (deterministic `elicit_codex_…` id) |
| **claude-sdk** | runner relay only — harness emits `policy_evaluation.requested` SSE; runner posts evaluate. Plus `RunnerToolPolicyGate` MCP fast-path for function-type tool policies | runner relay / server `_evaluate_input_policy` at `POST /events` | `RunnerToolPolicyGate` fast-path → server escalation; relay for LLM phases | relay (fail-open) | **`approval` event** → runner `pending_approvals` Future (`runner/policy.py:23`) |
| **codex (sdk)** | same as claude-sdk (relay + MCP fast-path) | server `_evaluate_input_policy` | fast-path / relay | relay | **`approval` event** → runner Future |
| **Polly** | inherits its underlying harness's row (Polly = custom agents on a harness) | per harness | per harness | per harness | per harness |

- **mcp__omnigent__\*** tools are skipped by the native hook (`native_policy_hook.py:244`) — already
  gated by the relay path (`ProxyMcpManager` → `/mcp` → `_evaluate_tool_call_policy`). **Connector-native**
  `mcp__github__*` etc. still go through the hook.
- **No keystroke emulation** for any in-scope harness (grep of claude/codex hooks: empty). Verdicts come
  back via held HTTP (native) or `approval` event (SDK). Out-of-scope native harnesses (goose/hermes/pi)
  use tmux-keystroke / external-resolved mirrors — not relevant here.

## 7. The ASK flow end-to-end

Two parking mechanisms, same wire shape:
- **Server-side held gate** `_hold_native_ask_gate` (`sessions.py:4119`) — for native PreToolUse + the
  REQUEST input gate (no runner in the loop yet). Publishes `response.elicitation_request`, parks the
  Future via `_publish_and_wait_for_harness_elicitation` (`:1397`), returns a bool. Wait ends on first of:
  (1) web verdict Future, (2) terminal-resolved Event (a mirrored tool result proves the TUI answered),
  (3) disconnect/timeout. Only (1) yields accept; (2)/(3) → DENY (fail-ask).
- **In-process workflow gate** `_await_elicitation` (`approval.py:80`) — registers a `__elicitation__`
  pending-tool row, emits on the root task SSE, parks on the `tool_result` topic.

End-to-end (native TOOL_CALL ASK):
```
PreToolUse hook ─POST /policies/evaluate─► engine.evaluate → ASK
  → _native_ask_gate_lock(session, deciding_policy)   # serialize siblings hitting same checkpoint
  → rebuild engine + re-evaluate under lock (a sibling's approval may have collapsed it)
  → still ASK → _hold_native_ask_gate
       → publish response.elicitation_request (mode:"url" → /approve/{sid}/{eid})  ──► web ApprovalCard
       → park Future
  web APPROVE → POST /elicitations/{eid}/resolve → _harness_elicitation_registry[eid].set_result(accept)
       → publish response.elicitation_resolved (badge clears, sidebar count--)
       → forward approval → runner
  gate returns True → apply withheld set_labels/state_updates (POLICIES.md §7.2) → hard ALLOW in held body
```
- **`ask_timeout` → DENY**: `resolve_ask_timeout` (`approval.py:264`, per-policy override else engine
  default); on expiry the Future race returns None → `_hold_native_ask_gate` returns False → DENY.
  Per-session/per-user/subtree cost approvals are routed to the **root** conversation /
  user-daily store so one approval covers the spawn tree (`engine.py:574`, `:599`).
- **TOOL_RESULT ASK is collapsed to DENY** in the runner fast-path (`runner/policy.py:184`) — the output
  already exists, no clean rollback.
- **DENY-on-deny invariant**: on decline/cancel/timeout/malformed verdict, withheld writes are dropped —
  a denied ASK leaves no trace (`approval.py:144`, `_hold_native_ask_gate:4212`).

## 8. Read-only (LEVEL_READ) eval

`POST /policies/evaluate` computes `is_read_only = level < LEVEL_EDIT` (`sessions.py:16005`) and calls
`engine.evaluate(ctx, read_only=True)`. Read-only path (`engine.py:284`, the `read_only` branches):
policies still run and the composed result still carries `set_labels`/`state_updates`, but **nothing is
persisted** and a read-only caller **never enters the ASK gate** (`sessions.py:16130`) — parking would
create an elicitation (a mutation). Lets a `LEVEL_READ` collaborator audit "what would be denied".

## 9. Policy creation & enforcement levels

- **Session-level**: `sys_add_policy` tool → `POST /v1/sessions/{id}/policies` (`session_policies.py:148`),
  `source="session"`, requires `LEVEL_EDIT`. Handler validated against the **registry allowlist**
  (`is_registered_handler`, `:181`) — an unregistered dotted path is rejected (RCE guard; admin must add
  the module via `policy_modules`). `factory_params` validated against the registry schema. Dup name → 409,
  bad params → 400. `type` is **immutable** on PATCH (`:285`). Activates on next engine build.
- **Admin / server default**: `POST /v1/policies` (`default_policies.py:129`, `_require_admin`),
  `session_id=NULL`, applies server-wide; `GET` is read-only-authenticated. Surfaced in the session list
  with `source="admin"` (`session_policies.py:239`).
- **Spec-declared**: agent YAML `guardrails.policies:`, `source="spec"`, `id=None`, **immutable**
  (cannot PATCH/DELETE — `_spec_to_response` `:72`).
- **Enforcement levels**:
  - *Server*: the authoritative engine. REQUEST (SDK/web at `POST /events`), TOOL_CALL/TOOL_RESULT relay,
    **LLM_REQUEST/LLM_RESPONSE (server-only, advisory, fail-open)**, and the elicitation registry all live
    server-side. The native hook posts here for every native event.
  - *Runner*: `RunnerToolPolicyGate` (`runner/policy.py`) is a **fast-path for function-type
    TOOL_CALL/TOOL_RESULT only** — ALLOW/DENY decided locally before MCP dispatch; **ASK escalates** to the
    server (`evaluate_policy=True`) which owns the elicitation channel. `label`/`prompt` types stay
    server-side (need the store / LLM classifier). The dual eval is intentional.

## 10. Reliability gaps / sharp edges (confirmed in code)

1. **LLM_REQUEST/LLM_RESPONSE have no runner-local gate** — only enforced if the harness emits
   `policy_evaluation.requested` (SDK relay) or via the native hook posting those phases; and they
   **fail-OPEN** on any outage (`runner/app.py:6270`, `FAIL_CLOSED_PHASES` excludes them). A cost/PII
   LLM-phase policy is silently skipped during a transient server outage.
2. **Native REQUEST gate is dedup'd by a heuristic, not an id** — a web prompt in flight is detected by
   `pending_inputs.snapshot_for` (`sessions.py:16065`); if that signal desyncs (see memory:
   native-firstmsg-fifo-desync), a web prompt could be re-gated (double-prompt) or a TUI prompt skipped.
3. **In-memory only** — `pending_elicitations` index and `_harness_elicitation_registry` are per-process;
   a multi-replica deploy each sees its own slice (`pending_elicitations.py:31`). The pre-resolved
   tombstone (`sessions.py:4006`) only patches the *same-replica* severed-long-poll gap.
4. **Hook token expiry** — historically the one-shot hook token lapsed (~1h) and the gate failed CLOSED
   on every tool call (memory: native-hook-token-expiry-failclosed). Now mitigated: `policy_hook_reauth`
   (`native_policy_hook.py:133`) re-mints on 401/302. But `pi_native` (Node hook) is the remaining gap
   (memory: native-hook-reauth-landscape) — out of scope here, flagged for completeness.
5. **`monotonic` without `values` asserts at runtime** (`engine.py:1224`) — a parser regression would
   500 the eval rather than degrade. Deliberate fail-loud.
6. **nessie shell classifier is a heuristic, not a boundary** — `_shell_statements` (`nessie/policies.py:134`)
   does not model subshells / `eval` / command substitution; a determined caller evades it. Sandboxing
   (OmniBox) is the real boundary; blast_radius is accidental-damage protection only.

## Corrections to CUJ-ANALYSIS §2.D

1. **Drifted line anchors** (verify-and-fix): resolve route is `sessions.py:18014` (CUJ says `:17611` — that
   is now an unrelated `_proxy_fs_response`); `_evaluate_tool_call_policy` is `sessions.py:10556` (CUJ says
   `:10384`); the evaluate endpoint is `sessions.py:15964`. `session_policies.py:148` and
   `default_policies.py:129` and `runner/policy.py` are **correct**.
2. **codex-native hook row is mislabeled.** CUJ's table puts codex-native on a single
   `codex-elicitation-request` hook. In code there are **two distinct paths**: the **policy** gate uses the
   *same shared* `PreToolUse/PostToolUse/UserPromptSubmit` hook as claude (`codex_native_hook.py:43` →
   `/policies/evaluate`, long-poll); the `codex-elicitation-request` endpoint
   (`codex_native_forwarder.py:3146`) is for codex's **own** permission prompt (`item/tool/requestUserInput`),
   not the policy ASK. The "verdict via long-poll HTTP" conclusion holds for both.
3. **"REQUEST/RESULT/LLM = fail-OPEN" is too coarse on two counts.** (a) **REQUEST fails CLOSED**, not open —
   it is in `FAIL_CLOSED_PHASES=("PHASE_TOOL_CALL","PHASE_REQUEST")` (`policies/types.py:61`;
   `native_policy_hook.py:426` blocks the prompt on an unreachable server). (b) The fail rule for a *raising
   policy* is by declared `action:` list, not phase — classifier-only `[allow]` fails ALLOW, `[ask]` fails
   ASK (`engine.py:1038`). The CUJ row conflates the transport-unreachable rule with the per-policy rule.
   (Minor: LLM_REQUEST/LLM_RESPONSE are correctly fail-OPEN.)
