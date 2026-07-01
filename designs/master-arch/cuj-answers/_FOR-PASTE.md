# How does claude-code / codex / polly behave when… — consolidated answers

> **Provenance:** grounded in code (`file:line` on branch `traces`, HEAD `60d11673`) + live Jaeger traces from a local rig (claude-sdk + claude-native traced live; **codex/codex-native NOT traced — Databricks AI-gateway creds expired/403**, answered from code + the §4 matrix and labeled). **Source-of-truth rule: code wins over the design docs** (`CUJ-ANALYSIS.md`/`OBSERVABILITY.md` line anchors had drifted thousands of lines in the 912 KB `sessions.py`; all anchors below are re-derived). **Polly** = a custom agent whose brain runs on **claude-sdk** (workers = claude-native + codex-native); unless noted it reads exactly as the claude-sdk row. Legend: ✅ confirmed · ⚠️ caveat/gap · ❌ absent.

---

## 1. Disconnects (mid-turn) — client + server side

**The turn keeps running; it is runner-side, not client-bound.** Client disconnect just drops the SSE socket. On the server, `_stream_live_events` (`sessions.py:11229`) has **no buffer and no replay**; its `finally` always yields `data: [DONE]\n\n` (`:11341`) so a clean close terminates cleanly. The reconnect contract is **GET snapshot + subscribe live tail, deduped by item id** (`:11240-44`). Disconnect detection: the SSE generator checks `request.is_disconnected()` per event + 15 s heartbeats; parked routes (native PreToolUse hook, elicitation long-poll) use `_poll_request_disconnect` (`:1183`) which *blocks* on `request.receive()` for `http.disconnect` and is raced against the verdict Future so a hung-up client releases the handler immediately. **Interrupt-fencing across reconnect:** `_interrupt_fenced_sessions` (`:931`) makes `_relay_runner_stream` drop a cancelled turn's trailing output (`:9464-75`) so a stopped turn never resurfaces in the durable store.
**Live (`conv_6bbdba9a`):** reopening the stream replayed the *identical* opening frames (heartbeat → resource.created → changed_files.invalidated → presence) = snapshot-on-connect, NOT token deltas. The one exception: **in-flight assistant text** is re-seeded synchronously at slot registration via `pre_ready_snapshot` (`:11315-20`) so the cursor doesn't blank.
**Per-harness:** identical at this layer (SDK + native). **Host-tunnel** disconnect is separate — runners keep running across a host blip; a runner that *dies* during the blip parks its `host.runner_exited` report and flushes after the next `host.hello` (`connect.py:1491-93`).

---

## 2. Forking a session — how the forked transcript is constructed

`POST /v1/sessions/{src}/fork` (`fork_session`, `sessions.py:15180`, 201, gate LEVEL_READ). **Server-side only** — the new conv returns `runner_id=null`, `host_id=null`; **no runner/harness is spawned**. `fork_conversation` (`sqlalchemy_store.py:2266`) **deep-copies items with fresh ids**, preserving `position`/`response_id`; `up_to_response_id` truncates the copy; it **drops instance-scoped labels** (native bridge ids, context-token metrics) and does NOT copy `external_session_id`/`workspace`/`git_branch`. Optional cross-family harness switch resets model/effort. **Cannot fork a sub-agent** (400, `:15238`). New session gets a **cloned agent** (new `agent_id`) + label `omnigent.fork.source_id`.
**Live (`conv_820c6dee → conv_7fe12aec`):** the fork showed as a burst of **~25 `INSERT` + many `SELECT` on chat.db** under the *source's* `session.id` — the synchronous deep-copy. **Fork cost scales with item count** (INSERT-per-item), visible as a latency spike, not a runner concern.
**Per-harness:** **native** targets carry history via the `FORK_CARRY_HISTORY` label so the vendor CLI rebuilds its transcript on the next turn (`harness-inner` §6); SDK targets reconstruct from the copied items. Host involvement only if `git` body → `host.create_worktree` for a new branch before the bind CAS.

---

## 3. Resuming a session — incl. how much transcript loads into the runner (native vs sdk)

**Two resume entrypoints by harness family.** **SDK (claude-sdk/codex/polly):** `omnigent run --resume` → normal create-session → bind-runner → dispatch. **Terminal-native (claude-native/codex-native):** `omnigent resume` is CLI glue (`resume_dispatch.py:39`) that reads `labels.omnigent.wrapper` and relaunches the matching `run_<harness>_native` (`:201`); an SDK session has no wrapper label → it errors and points you at `omnigent run --resume` (`:179`).
**How much transcript loads into the runner:**
- **SDK** — the runner reconstructs via `_load_initial_history` (`workflow.py:2276`): **full conversation, OR (when a compaction item exists) only the slice after the last `last_item_id` + the expanded summary pair**, then re-drives the SDK each turn with `executor.run_turn(messages=…)`. The in-process SDK keeps no store of its own, so "what loads" = the (possibly compaction-bounded) history. Visible in trace as `omni-runner → omni-server GET …/items` + the `persisted_item_id` dedup-drop.
- **Native** — resume keys on `external_session_id` from the snapshot; the **vendor CLI reloads its own session** (the vendor store is source of truth). Omni only re-injects when the vendor store is gone — cold resume synthesizes the vendor's local session file from committed Omnigent items (Pi `app.py:1715-29`; OpenCode injects a transcript preamble). So the runner loads ~nothing extra on a warm native resume; the vendor owns it.

---

## 4. Credential resolution — (a) provider selection at setup, (b) refresh of LLM / runner↔server / client↔server

**(a) Setup / provider selection.** `omnigent setup` wizard (`onboarding/wizard.py:1384`): 3 skippable steps — server URL, LLM auth (**API-key** vs **Databricks profile**, with detected `~/.databrickscfg` hints), default agent — written to `~/.omnigent/config.yaml` (`:1476`). **Ambient detection** (`ambient.py:619`) scans in priority: env API keys → Vertex → Claude CLI login (`~/.claude/.credentials.json` + Keychain) → Codex config provider → Codex CLI login (`~/.codex/auth.json`) → local Ollama. Provider kinds: `key`/`subscription`/`gateway`/`local`/`databricks`/`cli-config`/`bedrock`. DBX **profile aliasing** (`setup.py:190`) reuses a same-host profile's host-keyed OAuth cache (no redundant browser login). Per-harness default: claude→anthropic, codex→openai (`:1126`); subscription harnesses **don't inherit a parent's DBX profile** (`spec/omnigent.py:124`) so they keep subscription auth.
**(b) Refresh of the three relationships:**
| Relationship | Mechanism | Refresh | Status |
|---|---|---|---|
| **LLM creds** | claude-sdk: `_DatabricksBearerAuth.auth_flow` per request (`databricks_executor.py:367`); codex: provider `auth.command` shell on interval (`_GATEWAY_AUTH_REFRESH_MS=900_000`) + 401 (`codex_executor.py:763-98`) | **per request / 15-min interval** → survives ~1h OAuth | ✅ (static api-key/PAT/subscription = no refresh) |
| **runner↔server** | HTTP callbacks: `_RunnerDatabricksAuth.auth_flow` mints **fresh per request**, retries on 401/Apps-302 (`_entry.py:192`). **WS tunnel**: Bearer set **once at handshake** (`serve.py:540`) — **⚠️ no per-message refresh**; re-minted only on reconnect/401-drop | per-request (HTTP) / handshake-only (WS) | ✅ HTTP; ⚠️ WS gap (self-heals on recycle) |
| **client↔server** | cookie/JWT `__Host-ap_session` validated every request (`auth.py:351`, TTL cache); `omnigent login` writes JWT; DBX Apps = pointer record, token minted per use | validated per request; **⚠️ NO background refresh** — expired → re-login | ⚠️ no auto-refresh |

**Policy-hook token (native)** was the famous fail-closed-after-1h bug — **FIXED by PR #1439** (merged `e9561916`): claude-native + codex-native hooks now re-mint a fresh bearer on 302→/oidc or 401 and retry once (`policy_hook_reauth`). Fail-closed only if no token can be minted. (OpenCode's snapshot still fail-OPEN — out of scope.)

---

## 5. Request lifecycle (POST /events → … → response back to client)

`post_event` (`sessions.py:18150`, returns **202**). Order: **(1)** authz `_require_access_and_level(LEVEL_EDIT)` → **(2)** validate type ∈ `_ALLOWED_EVENT_TYPES` (`:18245`) → **(3)** closed-session guard (409 for a `sys_session_close`d sub-agent) → **(4) policy eval BEFORE persist** (`_evaluate_input_policy`, `:18321`; on exception → treat as **deny** so the session can't hang) → **(5) persist-before-forward (invariant I1)**: `conversation_store.append([item])` first (`:8540`), then POST to the runner over the reverse-tunnel (`:8697`), then publish `session.input.consumed` carrying the `item_id` (`:8704`). If a fresh item has **no runner + no host** → 503 `RUNNER_UNAVAILABLE` *before* persist (`:19068-76`).
Runner side (`app.py:14598`): FIFO ingest gate → content resolve → single-active-turn gate (I2) → `_load_history_as_input(drop_item_id=persisted_item_id)` → dispatch to harness over UDS → `proxy_stream` relays SSE chunk-by-chunk back to the server, which persists durable items and fans out to the client's `GET …/stream`.
**Live (`tree cfb59197…`, 415 spans):** `POST /events` → child `policy.evaluate` → `UPDATE`+`INSERT chat.db` (the append precedes the runner POST — **I1 visible on the wire**) → cross-edge to `omni-runner [POST …/events]` → runner calls back `GET …/items` + `GET …/agent/contents`.
**Per-harness:** **native** bypasses persist at dispatch (no server item id, only a `pending_inputs` `pending_id`); the transcript forwarder is the single writer.

---

## 6. MCP routing — custom (user) MCPs vs the `sys_*` MCP (who routes a call where)

**Server owns policy + routing; runner owns execution.** SDK loop: runner `ProxyMcpManager.call_tool` POSTs `POST …/mcp` (`proxy_mcp_manager.py:220`) → server runs TOOL_CALL policy → server POSTs back `POST …/mcp/execute` (`app.py:17829`). The split exists so tools run on the right machine/cwd/env. **The routing key is the `__` separator** (`app.py:17954`): **`{server}__{tool}`** (e.g. `github__search`) → **custom MCP** → `RunnerMcpManager` live stdio/HTTP subprocess (`:18011`); **no `__`** (e.g. `sys_os_read`) → **runner-local builtin** → `execute_tool` (`tool_dispatch.py`). Then TOOL_RESULT policy. So **both go through the same `/mcp → /mcp/execute` round-trip**; the only divergence is the dispatch fork.
**Live custom-MCP (`conv_4d0e6cce`, `echo__echo_shout`):** identical HTTP path to `sys_*`, gated by `policy.evaluate` TOOL_CALL + TOOL_RESULT; only the tool name reveals which executor. A custom stdio server runs **in the runner process tree** (its blast radius/creds are the runner's).
**Per-harness (native):** `mcp__omnigent__*` (`sys_*`) go through the in-turn relay (already policy-checked) so the **PreToolUse hook SKIPS them** to avoid double-eval (`native_policy_hook.py:209-11`); connector/custom MCP (`mcp__github__*`) still hit the hook → `/policies/evaluate`. Out-of-turn workspace access uses a separate `serve-mcp` exposing only `sys_os_*` (no sandbox).

---

## 7. Elicitation/permission hooks — which embedded, which REQUIRED for all policies, how verdicts return

| Harness | Required hooks (for *all* policies to enforce) | How verdicts return |
|---|---|---|
| **claude-native** | **PreToolUse** (→ `/policies/evaluate` TOOL_CALL) + **UserPromptSubmit** (→ REQUEST; sole native input gate) + **PermissionRequest** (→ `/hooks/permission-request`) | **long-poll HTTP** — verdict in held response body (`hookSpecificOutput.decision.behavior: allow\|deny`); server holds the `/policies/evaluate` long-poll and collapses ASK→hard ALLOW/DENY so a permissive permission_mode can't auto-approve (`sessions.py:16113`) |
| **codex-native** | shared `native_policy_hook` PreToolUse/UserPromptSubmit-equivs + **`codex-elicitation-request`** | **long-poll HTTP** (`sessions.py:16214`). **No live trace (creds 403).** |
| **claude-sdk / codex(SDK) / polly** | no command hooks — SDK calls server in-process; server `type=approval` event | runner **`pending_approvals` Future**; SDK `can_use_tool` callback runs an elicitation handler for connector-native tools. codex(SDK) surfaces **none** (`approvalPolicy:"never"`, `:1427`) |

**No keystroke emulation for any in-scope harness** — long-poll HTTP (native) or approval-event→Future (SDK). The single shared shape-translator is `native_policy_hook.py`. **The required input gate for native is UserPromptSubmit**, not the server `/events` gate (which is deduped for native via `pending_inputs`).
**Live ASK/DENY (`conv_c8a81cbd`, `ask_on_os_tools`):** `policy.evaluate decision=ASK` → publish `response.elicitation_request` (mode=url, `/approve/{sid}/{eid}` capability URL) → park the tool-call (turn stays `running`) → client POSTs `ElicitationResult{action}` to the resolve URL → unblocks. The ASK prompt is baked into `policy.reason`. DENY (`decline`) surfaces to the model as a `function_call_output` denial — it does **not** error the turn. Same verdict can arrive via `{"type":"approval"}` on `/events`; both route through `_resolve_elicitation` (`:3921`). ⚠️ native PreToolUse fails CLOSED on an unmintable token (pre-#1439 bug).

---

## 8. Dedup — server / runner / client side

**Server: no content-based dedup at the store.** `append` (`sqlalchemy_store.py:1411`) mints a fresh globally-unique `item_id`; dedup is id-based downstream, seeded by server-assigned ids. The server forwards `item_id` as `persisted_item_id` (`:8618`); `response_id` is the turn-grouping key stamped on every item (`:1483`), used to pair a `function_call` with its `function_call_output` (`tool_call_response_ids` map, `:9560-80`). **"There is no server-side dedup" — `app.py:5844`.**
**Runner: three seams (no global response-id cache).** (1) per-conversation **FIFO ingest gate** + single-active-turn gate preserve order / prevent overlapping turns; (2) **history-load dedup** drops the just-arrived `persisted_item_id` (`app.py:14842`); (3) **mid-turn injection exactly-once** via `injection.consumed` markers (`:14251`); native reader keeps an in-memory seen-set + generation-id dedup. The runner does **not** dedup outbound by item-id.
**Client: by `ctx.itemId`.** Web merges durable snapshot items + live SSE deduping on `ctx.itemId` (`chatStore.ts:15`, enforced `:1455/:1904-07/:2460-63/:2961-66/:3004-08`); per-response `seenCallIds`/`seenResultCallIds` in `blockStream.ts` drop the claude-sdk MCP **double-emit** (inline + post-stream flush). **TUI does NOT use itemId** — it dedups by **byte-equal text, multiset consume-on-match** (`_TurnProseTracker`, `_repl.py:2787-883`) because streaming deltas carry no id.
⚠️ **Native FIFO/pending-input desync:** no server item id for native web messages, only `pending_id` + `cleared_pending_id` — double-bubble risk if client dedup mis-orders (CUJ-ANALYSIS §6).

---

## 9. TUI vs WebUI state

Both consume the **same `response.*`/`session.*` SSE vocabulary** over the same `GET …/stream` + write to the same `POST …/events`; the web even hand-ports the Python `_sse.py`/`_stream.py` reducers. Differences: **(a) push planes** — web adds **`WS /v1/sessions/updates`** (sidebar watch-set: snapshot/changed/removed/heartbeat) + `/health` polling; **the TUI has no WebSocket at all** (only per-session SSE). **(b) dedup** — web by `ctx.itemId`, TUI by byte-equal-text multiset. **(c) surface** — web has sidebar/projects/sharing/comments/policies-admin/presence/files/terminals panels + `switch-agent`; the TUI has none of these (resume picker + event tape instead). **(d) durability of a sent message** — non-native: persisted at POST (synchronous `item_id`); native: NOT persisted at POST, round-trips the vendor TUI and reconciles via the forwarder's `session.input.consumed`. **(e) working state** — web shows a sidebar badge (awaiting>running) + chat indicator; TUI shows an inline spinner+timer from the *same* `session.status`. ⚠️ **TUI emits no traces today** (`telemetry.init("omni-tui")` is never called — one-line fix; mechanism is wired).

---

## 10. "Working" state — how computed + how it propagates

**Single server funnel:** `_publish_status(session_id, status, …)` (`sessions.py:5343`), `status ∈ {idle, running, waiting, failed}` (Pydantic-validated, fails loud). It atomically (1) writes the in-memory `_session_status_cache` (`:5391`) that the **sidebar** reads, and (2) publishes a `session.status` SSE event. **Origins:** SDK — runner emits `session.status` → `_relay_runner_stream` re-publishes via `_publish_status` (`:9447-519`); native — forwarder POSTs `external_session_status` → validated → `_publish_status` (`:18667-726`); deny path publishes running→idle around the sentinel. **Stickiness invariant:** a cached `failed` is NOT overwritten by a trailing `idle` (`:5389`) — only the next `running` clears it (prevents native StopFailure→failed being erased by a ~1s-later PTY-idle).
**Propagation:** sidebar reads the cache (WS updates frames carry `status` + `pending_elicitations_count`); the open chat reads `session.status` SSE (authoritative). Web chat adds `waiting` (parent parked on async-work drain) which the badge can't show; `background_task_count` is **sticky** so a claude-native turn with leftover shells reads "N background tasks running" (`chatStore.ts:3601-18`).
**Live:** an interrupted turn's fingerprint = **`error.type=cancelled`** on the `agent:` span (`runtime/telemetry.py record_cancellation`) while `otel.status_code` stays OK. ⚠️ `_session_status_cache` is **per-process** → multi-replica sidebar may read another replica's status (open question). ⚠️ **A session is inert until a runner is bound** — the "runner-offline → stuck working" gap class lives exactly here.

---

## 11. Transcript reconstruction — compaction; local↔server mismatch; fork/resume

**Compaction** (`runtime/compaction.py:544`, budget = `context_window*0.8 − system_budget`, recent window = 5 LLM groups): **L1 surgical clear** (tool-result bodies → placeholder, keep `file_id`) → **L2 LLM summary** (synthetic user+assistant pair, **routed through the runner's `POST /v1/summarize`** so the runner's creds are used) → **L3 emergency truncate** (pair-aware). **Triggering:** *proactive/threshold* in the in-process loop (`_call_llm_maybe_compact`, `workflow.py:2057`, force=False at 0.8) is the real auto-compaction; *reactive* harness-reported overflow **does NOT auto-compact** — `proxy_stream` raises `_ContextWindowOverflow` → `_run_turn_bg` **ends the turn with an error** (`runner/app.py:13804`, OMNI-143). Explicit `/compact` → `compact(force=True)`.
**Reconstruction:** `_maybe_persist_compaction_item` appends a `type=compaction` item (idempotent by `response_id==task_id`); next turn `_load_initial_history` loads only items after `last_item_id` + the expanded summary (`compaction_to_history_items`). Broken item → ignored, full reload (fixes #1082). **Fork** = deep-copy with fresh ids (Q2). **Resume** = SDK reconstructs the (compaction-bounded) history; native rebuilds from the vendor store (Q3).
**Streaming↔durable merge** (the local↔server reconcile): web walks one flat `blocks[]` from durable `GET …/items` + live SSE, deduped by `ctx.itemId`; a streamed id-less assistant `message` gets its item id **stamped onto the existing streamed block in place** (`chatStore.ts:3027-69`) so reconnect sees it as already-rendered.
**Live `/compact` (`conv_63542a5f`):** on a **model-less session** it returned `invalid_input "Compaction requires a configured LLM model"` (confirms **#1192** — a no-`--model` subscription session has no model row; gated *before* any summary work). Auto-compaction in the turn loop is unaffected (uses the turn's resolved model).
⚠️ **claude-sdk does NOT persist thinking** — `compacted_messages` keeps only content blocks (`:2554-58`); reasoning is regenerated on resume.

---

## 12. API routes & message formats — WS vs REST, which stream vs durable, reasoning

**54 sessions-router routes** (handler names AST-verified). Key write/read: `POST /v1/sessions` (create; JSON=existing agent, multipart=bundled), `POST …/events` (the turn entrypoint; message/control/external), `GET …/stream` (SSE live tail), `GET …/items` (paginated transcript), `GET …/{id}` (snapshot/reconnect), `PATCH …/{id}` (rename/archive/model/effort/runner-rebind/labels), `POST …/fork`, `POST …/switch-agent`, `POST …/elicitations/{eid}/resolve`. **WS (client↔server):** `WS /v1/sessions/updates` (sidebar watch-set), `WS …/terminals/{tid}/attach` (xterm↔tmux). **Server-side ingress tunnels:** `WS /v1/runners/{id}/tunnel`, `WS /v1/hosts/{id}/tunnel` (the latter carries **JSON control frames, not HTTP**).
**Events envelope:** `POST /events {"type":"message","data":{"role":"user","content":[{"type":"input_text","text":"…"}]}}`; control: `{"type":"interrupt"}`, `{"type":"compact"}`, `{"type":"approval"}`.
**STREAM vs DURABLE:** **durable** (persisted) = only `response.output_item.done` carrying a `message`/`function_call`(status completed only)/`function_call_output`, plus `compaction` items, plus resource-lifecycle + routing-decision items (`_extract_persistent_item_from_sse`, `:8930`). **Transient (SSE-only):** all `session.*` lifecycle/presence, the `response.*.delta` family, reasoning deltas, the Responses-API turn lifecycle, elicitation events, heartbeats. Text deltas accumulate (`text_acc`) and flush to a durable message only at a function-call boundary or terminal. **Three name families:** `response.*` (turn/output, mirrors OpenAI Responses API), `session.*` (Omnigent session/sidebar/presence), `external_*` (the *input* vocabulary a native forwarder POSTs into `/events`).
**Reasoning handling:** streamed as `response.reasoning_text.delta` (transient); **SDK recomputes/doesn't store** (claude-sdk keeps only content blocks); **native persists** (the vendor records reasoning in its store, the forwarder mirrors it). codex(SDK) emits **no** `CompactionComplete`; claude-sdk emits it *with* `compacted_messages`.

---

## 13. Harness-specific features — min features, model+effort at start AND mid-session, propagating user config, default resolution

**Min capability matrix (code-verified, base defaults ❌ except tool-calling):**
| | interrupt | queue | subagents | reasoning effort | elicitation | mid-session model |
|---|---|---|---|---|---|---|
| claude-sdk | ✅ `client.interrupt()` (`:1477`) | ✅ (`:1614`) | ✅ via `sys_session_*` | ✅ {low,med,high,xhigh,max} | ✅ SDK callback | ✅ `set_model()` next turn |
| codex(SDK) | ✅ `turn/interrupt` (`:2243`) | ✅ (`:2240`) | ⚠️† `CODEX_HOME` isolation | ✅ {none..xhigh} | ❌ `approvalPolicy:"never"` | ⚠️ **resets thread** (model in signature) |
| claude-native | ✅ bridge `Escape` (`bridge:2530`) | ✅ tmux inject | ✅ `subagents/*.meta.json`→`external_subagent_start` | ✅ `/effort` inject ⚠️none/minimal skipped | ✅ PreToolUse+PermissionRequest long-poll | ✅ `/model` inject + statusLine mirror; next turn |
| codex-native | ✅ `turn/interrupt` (`exec:116`) | ✅ `turn/steer` | ✅ `thread_spawn`→`external_codex_subagent_start` | ✅ `thread/settings/update` | ✅ `codex-elicitation-request` long-poll | ✅ `thread/settings/update`; next turn |

claude-sdk is the **only** in-scope harness with `supports_tool_boundary_interrupt`→✅ (`:1617`). **polly** = claude-sdk brain (reads as that row) + claude-native/codex-native workers.
**Model+effort at start:** NewChatDialog → `request.model_override` → `ExecutorConfig`; native bakes `--model`/effort into spawn flags. **Mid-session (web UI):** claude-sdk = next-turn config; codex(SDK) = closes app-session + new thread; claude-native = keystroke `/model`/`/effort` into tmux + statusLine mirror **back** every poll (best-effort, never the running turn); codex-native = `thread/settings/update` RPC. ⚠️ claude-native `/effort none|minimal` is persist-only.
**Propagating user's own harness config:** **claude-native `use_claude_config=True`** (`claude_native.py:349`) skips DBX/ucode auth and uses the user's own `~/.claude/` (creds, settings.json, MCP, hooks) — strongest passthrough. **codex-native** inherits `~/.codex/config.toml`+`auth.json` via a private CODEX_HOME mapping back to real home. SDK harnesses get config via `HARNESS_*` env (claude-sdk strips `ANTHROPIC_API_KEY` to keep subscription auth).
**Default model/provider resolution chain:** CLI `--model` → `OMNIGENT_MODEL` → YAML `executor.model` → config.yaml provider default → per-harness fallback (ad-hoc = `databricks-gpt-5-4`). Per-harness fallback: claude-sdk → `databricks-claude-opus-4-8` on the DBX gateway (⚠️ **#1128** — Opus billed when Sonnet intended but override arrived None, `:1910`); codex → `databricks-gpt-5-5`/`gpt-5.4-mini`; native → the vendor CLI's own default unless `--model` set.

---

## 14. Client reconciliation of streaming vs durable; close page & return

**Reconciliation** = one flat `blocks[]` fed by (a) durable snapshot items (`GET …/items`, each carries `ctx.itemId`) + (b) live SSE (`BlockStream.reduce`, token blocks id-less), **merged deduping on `ctx.itemId`**. Optimistic user bubble held as `PendingUserMessage{tempId}` until `session.input.consumed` promotes it (matched by `clearedPendingId` → FIFO head → fresh), reusing the tempId as React key (no remount). A streamed id-less assistant message gets its item id stamped onto the existing block in place.
**Close page & return** = **server-durable; the turn keeps running.** On return: `switchTo` opens SSE `…/stream` **first**, then fetches slim snapshot + initial history window concurrently and merges by item id (the stream-then-snapshot contract, `chatStore.ts:1825-907`). The SSE pump auto-reconnects (instant if previously healthy; `reconcileOnReconnect` pages backward until the window overlaps and recovers `sessionStatus`/`activeResponse` so a gap-completed turn doesn't strand the spinner). **Host offline → `ReconnectSessionDialog`** (`host_offline` shows the CLI reconnect command; `local_stranded` shows `--resume`; resumable managed hosts read `host_asleep`/`starting` and wake on next message, no dialog). Background-tab nuance: on `visibilitychange→visible` the store reconciles pending elicitations against a fresh snapshot.
**TUI** has the same durability story minus the dialog — the SSE pump reconnects, server keeps running, reconcile via `sessions.get`.

---

## 15. Role of the executor

An **Executor** (`inner/executor.py:518`) is the per-vendor adapter translating **Omnigent's tiny abstract turn model ↔ a concrete vendor SDK**: in = `run_turn(messages, tools, system_prompt, config)`; out = an async stream of `ExecutorEvent`s (`TextChunk`, `ReasoningChunk`, `ToolCallRequest`, `ToolCallComplete`, `TurnComplete`, `CompactionComplete`, `TurnCancelled`, `ExecutorError`). Capability predicates (`supports_streaming`, `handles_tools_internally`, `interrupt_session`, …) default ❌ except `supports_tool_calling`. It runs inside a **per-conversation harness subprocess** (`omni-harness`); the **`ExecutorAdapter`** (`harnesses/_executor_adapter.py:141`) bridges: lazily constructs the executor, translates `CreateResponseRequest`→messages+config, calls `run_turn`, and per yielded event emits typed SSE; it round-trips spec/MCP tools the SDK can't run via `dispatch_tool` (parked Future keyed by `call_id`).
**Live:** the loop **is** the `agent:<harness>` AGENT span on omni-harness wrapping `run_turn()`, with `tool:` spans nested under it. ⚠️ **No `llm_call [LLM]` span for the SDK path** — `start_llm_span` exists but no executor calls it; the adapter subsumes the LLM call into the one AGENT span (verified: zero `llm`-named spans in Jaeger). Real nesting = agent → tool; guardrails are a separate `policy.evaluate` span on omni-server; a sub-agent's AGENT span is a separate trace.
**SDK vs native:** SDK `run_turn` = full loop+tools; **native `run_turn` injects the latest user message and yields `TurnComplete(response=None)` immediately** — the vendor CLI runs the real loop and the forwarder mirrors it back.

---

## 16. Subagent spawning + depth limits

**Declaration:** `AgentTool` (by name or inline) or `SelfAgentTool` (`tools.<name>: self`, clones parent with self-tools removed = the self-recursion guard) → nested `AgentSpec` (`spec/omnigent.py:1090-120`). **Runtime spawn (SDK):** the LLM calls **`sys_session_send`** (`tools/builtins/spawn.py:56`) — mode A `(agent,title)` **mints a child Conversation** + starts a turn; mode B `session_id` posts to an existing direct child. The child's `parent_session_id` = the immediate parent, **inherits the caller's runner** (co-location), and **runs its own turn loop** (own omni-harness, own AGENT trace). `sys_session_send` is **confined to direct children** — no sibling channel. Results return via the `async_work_complete` inbox.
**Depth limits — VERIFIED display-only, NO spawn-time cap.** `_MAX_SUBAGENT_TREE_DEPTH=3` (`repl/_repl.py:201`) caps only how deep the **REPL sidebar renders** the tree. No spawn-time depth check exists anywhere; ordinary `AgentTool` chains can recurse unbounded (`AgentTool.max_sessions` is a per-tool *concurrency* cap, not depth). ⚠️ **real runaway-recursion risk** (CUJ-ANALYSIS §6).
**Live (`conv_387e2405`, debby → claude + gpt children):** trace exposed `tool:sys_session_send` (×2, one per child) → children created via runner→server `POST …/sessions` (each a **full session with its own session.id**) → `tool:sys_read_inbox` drains results → `GET …/child_sessions` (the rail API). The gpt(codex) child failed on creds; the claude child answered — debby tolerated partial failure. **No single trace spans the tree** — stitching requires collecting all child session.ids.
**Per-harness (native):** children minted via `external_subagent_start` (claude-native, `fwd:217/1115`) / `external_codex_subagent_start` (codex-native, `fwd:6079/4304`). ⚠️ **#848** — native sub-agent completions never reach the orchestrator (gate `runner/app.py:12607` excludes native).

---

## 17. Inbox mechanics

**`sys_call_async(tool, args)`** (`async_inbox.py:129`) dispatches a **local Python tool** as a background task (`is_async()` always True), returning an `_AsyncToolHandle` `{task_id, status:"in_progress"}`. The blocking **drain lives in the harness subprocess**, on topic **`async_work_complete`** — two consume-once paths: **auto-drain** at the top of every loop iteration (`_drain_async_completions`, delivers piled-up payloads as `[System: task …]` user messages, `:253`) and **`sys_read_inbox`** (pull, mid-turn, returns a string `function_call_output` so the LLM can fan out a second wave without waiting, `:240-308`). Both remove payloads so a completion is never seen twice. **Subagent-completion delivery** runs runner-side in `_on_proxy_stream_end` (`app.py:12519`) → `_deliver_subagent_completion` pushes the child's (truncated) output to the **parent's inbox queue** + schedules a wake-POST.
⚠️ **`sys_cancel_task`/`sys_cancel_async` are NO-OPs** — the tasks table was removed; they return `{"error":"task_not_found"}` for every input (`:97-126`). Async/subagent cancellation is effectively broken (Bash background jobs are killed via the SDK's KillBash instead). ⚠️ native children skip the completed-delivery gate (#848).
**Live:** the debby trace's `tool:sys_read_inbox` (×2) confirmed the consume-once drain (`async_work_complete`).

---

## 18. OmniBox (the OS sandbox)

**OmniBox = the OS-level sandbox**, not a web component (`inner/sandbox.py:resolve_sandbox:371`). `OSEnvironment` modes: `caller_process` (no isolation) · `fork` (workspace copy) · `sandbox`. **Backends:** `linux_bwrap` (bubblewrap — mount/PID/UTS/IPC namespaces, ro-bind roots, tmpfs-masked dotfiles, hardened seccomp denylist), `darwin_seatbelt` (`sandbox-exec` SBPL `(deny default)`, no namespaces/seccomp), `windows_jobobject` (process-tree containment, no FS/net isolation), `none`. Missing backend binary → **fail-loud at build** (never silently unsandboxed). **3 isolation layers:** (1) **filesystem** — only granted paths visible, dotfiles masked; (2) **default-deny L7 egress proxy** (`inner/egress/`) — DSL allowlist (default deny), DNS-safe host regex, **private-destination block** (RFC1918/loopback/CGNAT-100.64/cloud-metadata 169.254.169.254 traps; **resolve-once defeats DNS rebinding**), MITM per-sandbox CA over a Unix socket; (3) **credential injection** (`credential_proxy.py`) — **swap-on-access default** (nothing credential-shaped in the sandbox; proxy injects the real Authorization for the bound host), opt-in `oa_cred_*` placeholder, wrong-host→403; the real secret lives only in parent+proxy, **never** serialized into `SandboxPolicy`/argv/disk.
⚠️ **Sandboxed claude-sdk on macOS crashes** vs degrading (#517 part-1, no PR). ⚠️ `credential_proxy` parent-side `subprocess.run(shell=True)` + arbitrary file reads on a trusted-spec assumption (#1542, no PR). **Code-only — no live sandbox trace** in this rig.

---

## 19. Web UI sidebar fetching + the FULL set of client→server requests

**Sidebar:** `useConversations` (`hooks/useConversations.ts:216`) = TanStack `useInfiniteQuery` over `GET /v1/sessions` (`order=desc, sort_by=updated_at, limit=20`, optional `search_query`/`include_archived`, cursor-paginated by `last_id`). **Live updates via `WS /v1/sessions/updates`** (replaces the old 4s poll): client sends `{type:"watch", session_ids:[…]}` (union of cached rows + open session); server pushes `snapshot`/`changed`(status, runner_online, host_online, pending_elicitations_count, title)/`removed`/`heartbeat`. Frames patch cache in place; only structural changes schedule a debounced invalidate. 70s heartbeat watchdog; HTTP fallback 60s connected / 45s disconnected. Grouping precedence: **Archived > Pinned > Project > Recent**. Badge priority **awaiting > running > none**.
**FULL client→server set** (web): **REST sessions** — `GET/POST /v1/sessions`, `GET …/projects`, `GET/PATCH/DELETE …/{id}`, `POST …/events`, `POST …/fork`, `POST …/switch-agent`, `POST …/elicitations/{eid}/resolve`, `GET …/items`. **Sub-resources** — `…/agent`, `…/permissions`(GET/PUT/DELETE), `…/owner`, `…/comments`(GET/POST/PATCH/DELETE/send), `…/policies`(GET/POST/DELETE), `…/resources/terminals`(GET/POST), `…/resources/files`, `…/resources/environments/default[/changes|/filesystem|/search]`, `…/codex_goal`(GET/PUT/PATCH/DELETE + /status). **Fleet/hosts/caps** — `GET /v1/agents`, `GET /v1/runners`, `POST /v1/hosts/{id}/runners`, `GET /v1/hosts/{id}/filesystem`, `POST …/directories`, `GET /v1/policy-registry`, `GET/POST /v1/policies` + `PATCH …/{id}`, `GET /v1/info`, `GET /v1/me`, `GET /health?session_ids=`. **Accounts/auth** (only when enabled, bare cookie fetch): `/auth/{login,logout,me,register,setup,invite,users…}`. **WS/SSE:** `GET …/stream` (SSE chat tail), `WS /v1/sessions/updates`, `WS …/terminals/{tid}/attach`.
Note: `GET /v1/users/search` is **not** a direct SPA call — it's a host-injected callback (inert in standalone). ⚠️ **No live `omni-web` traces** (web telemetry opt-in, inactive in rig) — code-grounded.

---

## 20. Policy enforcement — how created; server-level vs session/runner-level

**Created from three sources, merged at engine-build** (`builder.py:309`, order **session → agent-spec → admin** + a hardcoded gate): **(a) session-level** — agent calls `sys_add_policy` (after `sys_policy_registry`) → `POST …/policies` (`session_policies.py:148`); python handlers **must be in the registry allowlist** (anti-RCE); activates immediately; `sys_add_policy` itself is unconditionally ASK-gated (`_ASK_ON_ADD_POLICY_SPEC`, `builder.py:64/315`). **(b) server/admin default** — `POST /v1/policies` (`default_policies.py:129`, `_require_admin`), `session_id IS NULL`, applies server-wide, appended last ("admin gets the last word"). **(c) spec-declared** (YAML `guardrails.policies:`) — immutable, `id=None`, can't be PATCHed/DELETEd.
**Enforcement: one engine** (`PolicyEngine`, `engine.py:43`), **two surfaces.** **Server-level** (engine in omni-server): REQUEST/RESPONSE gates at `/events`; TOOL_CALL/TOOL_RESULT at the `/mcp` proxy; the generic `/policies/evaluate` hook (native PreToolUse + LLM-phase). **Runner-level fast-path** (`RunnerToolPolicyGate`, `runner/policy.py:109`): runs **only function-type TOOL_CALL/TOOL_RESULT** policies (label/prompt types stay server-side — they need the store/LLM the runner lacks); **ALLOW/DENY decided locally before MCP dispatch**, **ASK escalates to the server** (which owns the elicitation channel); TOOL_RESULT collapses ASK→DENY. **5 phases**: REQUEST, TOOL_CALL (main gate), TOOL_RESULT (DENY/redact only), advisory LLM_REQUEST/LLM_RESPONSE. **First DENY short-circuits**; ASK accumulates. **Fail-CLOSED phases = `("PHASE_TOOL_CALL","PHASE_REQUEST")`** (`types.py:61`) → DENY on server outage; TOOL_RESULT/LLM_* fail OPEN.
**Live (`cfb59197…`):** **all 5 phases observed** as `policy.evaluate` spans — REQUEST + LLM_REQUEST as children of `POST /events`; TOOL_CALL + TOOL_RESULT as children of `POST /mcp`; LLM_RESPONSE at the end. The observed claude-sdk run routed TOOL_CALL/RESULT through the **server `/mcp` proxy** (because `sys_os_shell` is server-dispatched), not the runner fast-path. ⚠️ Permissions-disabled ⇒ `accessible_by=None` returns ALL sessions (cross-user leak risk). ⚠️ pending-elicitation badge is **in-process** → multi-replica splits it.

---

## 21. System resources — shells (+ working-dir resolution), how exposed, timers

**Two shell paths:** **`sys_os_shell`** — one-shot command in the agent's **shared `OSEnvironment`** (all four `sys_os_*` share one instance so cwd/sandbox/env stay consistent, `os_env.py:378`); **`sys_terminal_*`** — **persistent named tmux panes** (`TerminalRegistry.launch`, keyed `(conversation_id, terminal_name, session_key)`, survive across turns, `remain-on-exit on`). Both gated by spec (`os_env:` / non-empty `terminals:`). MCP tools are **not** registered in the ToolManager — they're runner-owned.
**Working-dir resolution** (`_resolve_cwd`, `sys_terminal.py:752`, first match): LLM `cwd_override` → `terminal.os_env.cwd` → `spec.os_env.cwd` → `ctx.workspace` → (implicit) host/runner cwd. The **host** resolves the runner's cwd: server sends a realpath-validated `workspace` in `host.launch_runner`; the host expands `~` (only it knows its `$HOME`) and passes `RUNNER_WORKSPACE`. Shells inherit from the runner, not the host. **Live:** the `sys_os_shell` TOOL_RESULT carried `cwd=/Users/.../traces` = the runner workspace (tier 4/5). **Reaping:** orphan reaper at runner startup (`tmux kill-server` for dead-owner-pid instances); idle native-pane reaper (30-min idle, #1349).
**Timers** (gated `timers:true`, default False): `sys_timer_set` returns a `timer_id` synchronously; on fire it **re-injects a turn via a synthetic `is_meta` `[System: timer X fired]` POST to `/events`** — it's a per-session runner-local `asyncio.create_task`, **not** a scheduler service. **Live (`conv_48ce846b`):** confirmed the full firing loop — `policy.evaluate tool=sys_timer_set` (set is gated) then the firing's `policy.content="[System: timer … fired]"` → a brand-new `agent:` turn. Timers die with the runner (no persistence). ⚠️ **sessions-native firing path raises `NotImplementedError`** (`timer.py:220`) — timers only work under the local-runner topology.

---

## 22. Custom agent's subagents init

A custom agent's subagents are declared as `AgentTool` (by name → a registered agent, or inline) or `SelfAgentTool` (clone of parent, self-tools removed), parsed into nested `AgentSpec` (`spec/omnigent.py:1090-120`). **Loaded with `prune_invalid_sub_agents=True`** (`agent_cache.py:94/146/204` → `spec/__init__.py:314`): depth-first, a sub-agent that fails validation on **this** (possibly older) server is dropped with a WARNING and its name removed from the parent's `tools.agents` — so version skew degrades gracefully and the parent still dispatches (authoring/upload validation stays strict elsewhere). **`AgentTool`/`SelfAgentTool` synthesis** inherits parent profile/harness/os_env/terminals and recurses into nested tools (`_agent_tool_to_sub_spec`, `:1319`). At runtime the agent spawns them via `sys_session_send` (Q16). ⚠️ `pass_history`/`pass_histories`/`max_sessions` are **lossy** (dropped to defaults) on the omnigent-compat translator path (`:1338-41`); first-class on the inner `AgentDef` path.

---

## 23. How custom agents are stored in the server

**Three tiers** (`runtime/agent_cache.py`): **(1) ArtifactStore** — content-addressed `.tar.gz` bundle (source of truth); **(2) Agent DB row** — `id`/`name`/`bundle_location`/`version`/`session_id` (session-scoped agents have a non-null `session_id`; template/registered agents null); **(3) AgentCache** (`agent_cache.py:16`) — Tier-1 in-mem `AgentSpec` (`_specs`) + Tier-2 on-disk extract `<cache_dir>/<agent_id>/`, **no TTL**. Miss → download → `load_spec` (extract+parse+validate) → both tiers. **Evict on delete** (`:161`, drops spec + rmtree); **warm-swap on update** (`replace`, `:102`: extract to `_staging`, atomic `_specs[id]=` reassign, rmtree old + rename staging — readers never see an empty cache; version bumps). **Security:** `expand_env=False` default — `${VAR}` expanded against server env **only** for operator template agents (`session_id is None`), **never** tenant/session agents (`:66-80`). Created via `POST /v1/sessions` multipart (`metadata`+`bundle`) → session-scoped agent + conv row in one txn. **Polly/debby** = registered custom agents stored exactly this way.

---

## 24. Harness switching

**`POST /v1/sessions/{id}/switch-agent`** (`switch_session_agent`, `sessions.py:15415`, gate LEVEL_EDIT) — **idle-only, 409 if running** (`:15481`). Loads the target bundle **before** committing (fail-closed); `switch_conversation_agent` (`sqlalchemy_store.py:2576`) deletes the old session-scoped agent, clones the target, repoints `agent_id`, resets model/effort on cross-family, and **clears `external_session_id`** (`:2663`) so a **native** target cold-starts (rebuilds its vendor transcript) next turn. Publishes `session.agent_changed`; resets runner resources in the background. **Fork** can also switch harness (optional), with native targets rebuilding from `FORK_CARRY_HISTORY`.
**Live (`conv_820c6dee` trace_probe→debby):** switch on an idle session returned immediately, bound a **cloned** target agent (new `ag_…`), **reset labels**, and **retained the runner binding** (`runner_token_c2a963…`). **The runner survives the switch** — the next turn reuses the same runner, which re-initializes for the new agent/harness (no new runner launch). **Polly** switches *workers* (spawns claude_code/codex/pi sub-agents), not its own brain harness. ⚠️ TUI has no switch-agent (its `/switch` only re-points the SSE stream to a different session).

---

## 25. Caching — agent cache + credential cache (what / TTL / invalidation)

| What | Where | TTL | Invalidation |
|---|---|---|---|
| **Agent bundle** (parsed spec + extracted dir) | `runtime/agent_cache.py:16` | **none** | explicit `evict()` on delete; `replace()` warm-swap on update (version bump) |
| **Provider model listing** (`sys_list_models`) | `model_catalog.py:61` TTLCache(64) | **5 min** | TTL; `clear_model_catalog_cache()` after reconfigure; failures NOT cached |
| **MLflow model catalog** (per provider) | `onboarding/providers/__init__.py:102` TTLCache(64) | **1 h** | TTL; `OMNIGENT_DISABLE_CATALOG_LOOKUP=1` |
| **Provider/credential resolution** (auth/base-url/profile) | resolved per call | **none** | recomputed fresh each spawn |
| **Runner DBX SDK auth** | `_make_auth_token_factory` closure (`_entry.py:322`) | SDK in-mem cache, re-shells near expiry | factory rebuilt → re-resolves |
| **Client cookie→user-id** | `server/auth.py:387-411` (HMAC-digest key) | token's remaining lifetime | TTL expiry; ⚠️ no revocation list |
| **Native session state / policy-hook token** | `bridge.json`/`policy_hook.json` | **one-shot snapshot** | re-created on relaunch; **re-minted on 401/302 (PR #1439)** for claude/codex native |
| **Inner LLM client singleton** | `workflow.py:251` | process-lifetime | none |
| **Status / usage / pending-elicitation** | server in-memory (`_session_status_cache` etc.) | process-lifetime | ⚠️ per-replica (no backplane) |

**Net:** the credential cache inside `runtime/` is essentially "none" — auth resolved fresh per spawn, per-request token refresh in the executors/runner callbacks. The two real caches are the **agent cache (no TTL, event-invalidated)** and the **model-catalog caches (5 min / 1 h)**.

---

## 26. Components to instrument + the channels between each

| Component | Process | Telemetry today |
|---|---|---|
| **Server** | FastAPI `omnigent/server/`, one per deployment | ✅ `omni-server` (FastAPI auto-instrument) |
| **Host daemon** | `omnigent host`, one per machine | ✅ `omni-host` |
| **Runner** | `omnigent.runner._entry`, one per session | ✅ `omni-runner` (cached server→runner client `instrument_httpx_client`'d) |
| **Harness/executor** | `omnigent/inner/*_executor.py` (+ native CLI) | ✅ `omni-harness` (the `agent:`/`tool:` spans) |
| **Web UI** | `web/src/` | ⚠️ wired (`lib/telemetry.ts:40`) but **opt-in/inactive** |
| **TUI/REPL** | `omnigent/repl/` | ❌ **`telemetry.init("omni-tui")` never called** — emits no spans (one-line fix) |
| **Policy "server"** | **no separate process** — in-server `policies/engine.py` + runner fast-path + native `/policies/evaluate` hook | ✅ via `policy.evaluate` spans |

**Channels (what to instrument between each):** Client→Server = **HTTPS REST + SSE** (`…/stream`) — FastAPI extract + httpx/browser inject (W3C). Client→Server sidebar = **`WS /v1/sessions/updates`** — manual `traceparent` in JSON envelope. Server⇄Runner = **WS reverse-tunnel forwarding HTTP verbatim** (server calls "into" runner; `traceparent` tunneled, custom-transport client instrumented directly — verified runner spans nest under server spans). Server⇄Host = **WS control frames (JSON, not HTTP)** — manual `traceparent` field, host opens `consume_frame_span(kind)` (verified `host.launch_runner`/`host.stat` nest under `POST /v1/hosts/{id}/runners`). Host→Runner = **one-way spawn env** (⚠️ **TRACEPARENT is NOT injected** → each runner roots its own trace). Runner→Harness = **HTTP over UDS**. Harness→Server = **JSONL forwarder re-POSTs `/events`** (own trace). Native harness↔Runner = **bridge HTTP relay (Bearer) + tmux**. Server⇄DB = SQLAlchemy.
**Critical cross-cutting fact:** **one user action ⇒ ~21 traces**, stitched only by **`session.id`** (the forwarder, host control plane, and runner spawn all root their own traces because TRACEPARENT isn't propagated into the spawn env). To instrument end-to-end you must correlate by `session.id`, not parent span.

---

### Summary — where answers are thin or code-only (no live trace)

1. **All of codex / codex-native is code-only** (Q4, Q5, Q7, Q13) — AI-gateway creds expired (403); a `DATABRICKS_BEARER`/PATH propagation gap in the runner spawn env (host reports `codex: needs-auth`). The server/runner/MCP path is harness-agnostic so it applies structurally, but no codex span exists in the corpus.
2. **OmniBox/sandbox (Q18)** is entirely code-grounded — no live sandbox trace in this rig; the macOS-crash (#517) and credential_proxy-RCE (#1542) gaps are unverified-at-runtime.
3. **Web UI (Q14, Q19) and TUI (Q9) have no traces** — web telemetry is opt-in/inactive and the TUI never calls `telemetry.init` (so `omni-tui → …` in the design doc doesn't exist today); client-side reconciliation is read from source only.
4. **Compaction (Q11)** — only the *rejection* path (#1192 model-less) was traced live; a real L1/L2/L3 compaction trace needs a model-pinned session exceeding the threshold. **Multi-replica** behavior (status cache, pending-elicitation badge — Q10/Q20/Q25) is an open question (in-process caches, no backplane confirmed).
5. **Thinly traced but code-confirmed:** the runner↔server **WS-tunnel no-per-message-refresh** gap (Q4) and the **native PreToolUse fail-closed** path (Q7) are confirmed in code + PR #1439 history but not exercised live; native sub-agent completion (#848, Q16/Q17) is code-confirmed only.
