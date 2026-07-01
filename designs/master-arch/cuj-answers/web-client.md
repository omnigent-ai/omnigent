# CUJ Answers — Web Client (`web/src/`)

> Code is ground truth; every claim is `file:line`-anchored. Web telemetry is
> opt-in (`lib/telemetry.ts`) and inactive in the running rig, so there are **no
> live `omni-web` traces** — this is a code-grounded analysis (confirmed: no
> `omni-web` spans in the saved corpus). Companion: `architecture/web.md`.

---

## Q1 — How the SIDEBAR fetches items

**Source list (`useConversations`, `hooks/useConversations.ts:216`)** — TanStack
`useInfiniteQuery` over `GET /v1/sessions`:
- Query params (`fetchConversationsPage`, `:178-204`): `order=desc`,
  `sort_by=updated_at`, `limit=20`, optional `search_query=<q>` (server-side
  filter; the caller debounces ~300 ms before passing it), optional
  `include_archived=true` (only when the toggle is on).
- Cursor pagination: `getNextPageParam = lastPage.has_more ? lastPage.last_id :
  undefined` (`:238-239`). 20 per page. Infinite-scroll sentinel in `Sidebar.tsx`
  (200 px pre-fetch margin) calls `fetchNextPage`.
- `sort_by=updated_at` matches the within-group sort, keeping server pagination
  consistent with visible order. The active chat's row is pinned in place by an
  in-memory `ActiveChatOverride` so a send doesn't reorder it (`:16-18`).

**Live updates — `WS /v1/sessions/updates`** (`lib/sessionUpdatesSocket.ts`,
`hooks/SessionUpdatesProvider.tsx`) replace the old 4 s poll:
- Client sends a **watch-set**: `{type:"watch", session_ids:[…]}`
  (`sessionUpdatesSocket.ts:242`). Ids = union of all `["conversations"]` +
  `["project-sessions"]` cache rows **plus the open session**
  (`SessionUpdatesProvider.tsx:159-178`). No-op when unchanged.
- Server pushes **`snapshot`** (full watch-set state), **`changed`** (field
  deltas: status, runner_online, host_online, pending_elicitations_count, title,
  comment fingerprint), **`removed`** (ids), **`heartbeat`** (~30 s idle)
  (`sessionUpdatesSocket.ts:23-27`).
- Frames patch the cache in place (`applyItemsToCache` →`mergeItemsIntoPages`);
  only structural changes (a watched id missing from every page, membership/sort
  changes) schedule a debounced `invalidate(["conversations"])` (250 ms)
  (`SessionUpdatesProvider.tsx:240-249`).
- **Heartbeat watchdog** `70_000 ms` (`:48`): no frame in 70 s → force reconnect.
  Reconnect backoff 250 ms→5 s ±50% jitter.
- HTTP fallback cadence: connected → `60_000 ms` low-rate reconcile for the
  visible sidebar only (others suspend); disconnected → `45_000 ms` for all
  (`useConversations.ts:41-42, 240-245`).

**Grouping precedence** (per `CUJ-ANALYSIS §2.E`): **Archived > Pinned > Project >
Recent**. Pins = localStorage `omnigent:pinned-conversation-ids`, off-window pins
backfilled via per-id `GET /v1/sessions/{id}` (`usePinnedConversationBackfill`,
`:619`). Projects implicit (≥1 non-archived session), reserved label
`omni_project` (`:658`), listed via `GET /v1/sessions/projects` (`:661`), folders
paged via `?project=` (`useProjectSessions`, `:786`).

**Badge** (`useSessionState.ts:21`): priority **awaiting (pending_elicitations_count>0)
> running > none**. `failed`/liveness deliberately NOT sidebar badges.

---

## Q2 — FULL set of client→server requests (exhaustive)

> Every distinct request the SPA issues, verified in non-test source. All HTTP
> goes through `authenticatedFetch` / `hostFetch`; all WS through
> `resolveWebSocketUrl`. `/auth/*` use bare `fetch` (cookie-based, accounts mode).

### REST — sessions core

| Method | Path | File:line | Trigger |
|---|---|---|---|
| GET | `/v1/sessions` | `useConversations.ts:201` | Sidebar list (paginated, search, archived) |
| GET | `/v1/sessions?project=<p>` | `useConversations.ts:727,753,771` | Project folder list / last-member check |
| GET | `/v1/sessions/projects` | `useConversations.ts:665` | Project names |
| GET | `/v1/sessions?limit=100[&kind=any]` | `useAgents.ts:82`, `useAvailableAgents.ts:168` | Custom/registered agents (sessions-as-agents) |
| POST | `/v1/sessions` (JSON) | `sessionsApi.ts:419` | Create session bound to `agent_id` |
| POST | `/v1/sessions` (multipart) | `sessionsApi.ts:455` | Create with inline agent bundle |
| GET | `/v1/sessions/{id}` | `sessionsApi.ts:715`, `useConversations.ts:151` | Full snapshot / pinned backfill |
| GET | `/v1/sessions/{id}?include_items=false&include_liveness=false[&refresh_state=true]` | `sessionsApi.ts:746` | Slim snapshot (bind fast path) |
| GET | `/v1/sessions/{id}/items?limit=20&order=desc[&after=]` | `sessionsApi.ts:789` | History page (hydrate / scroll-up) |
| PATCH | `/v1/sessions/{id}` | `sessionsApi.ts:659`, `useConversations.ts:250,267` | rename / archive / model_override / reasoning_effort / collaboration_mode / cost_control / runner_id / labels(project) |
| DELETE | `/v1/sessions/{id}[?delete_branch=true]` | `useConversations.ts:285` | Delete session (+worktree) |
| POST | `/v1/sessions/{id}/events` | `sessionsApi.ts:887` | message / interrupt / approval / stop_session / compact / slash_command |
| POST | `/v1/sessions/{id}/fork` | `sessionsApi.ts:520` | Fork/clone (`up_to_response_id`, `agent_id`, `model_override`) |
| POST | `/v1/sessions/{id}/switch-agent` | `sessionsApi.ts:546` | Switch harness/agent in place (idle only) |
| POST | `/v1/sessions/{id}/elicitations/{eid}/resolve` | `sessionsApi.ts:972` | Approval verdict (URL-based elicitation) |

### REST — sub-resources

| Method | Path | File:line | Trigger |
|---|---|---|---|
| GET | `/v1/sessions/{id}/agent` | `useAgents.ts:134` | Session's bound agent detail |
| GET | `/v1/sessions/{id}/permissions` | `permissionsApi.ts:96` | Share dialog: list grants |
| GET | `/v1/sessions/{id}/owner` | `permissionsApi.ts:102` | Owner (info popover) |
| PUT | `/v1/sessions/{id}/permissions` | `permissionsApi.ts:116` | Grant/update level (incl. `__public__` toggle) |
| DELETE | `/v1/sessions/{id}/permissions/{userId}` | `permissionsApi.ts:131` | Revoke |
| GET | `/v1/sessions/{id}/comments[?path=]` | `useComments.ts:47-48` | List file comments |
| POST | `/v1/sessions/{id}/comments` | `useComments.ts:82` | Create comment |
| PATCH | `/v1/sessions/{id}/comments/{cid}` | `useComments.ts:131` | Update (status/body) |
| DELETE | `/v1/sessions/{id}/comments/{cid}` | `useComments.ts:111` | Delete |
| POST | `/v1/sessions/{id}/comments/send` | `useComments.ts:164` | "Address All" → send comments to agent |
| GET | `/v1/sessions/{id}/policies` | `usePolicies.ts:35` | Session policies |
| POST | `/v1/sessions/{id}/policies` | `usePolicies.ts:83` | Add session policy |
| DELETE | `/v1/sessions/{id}/policies/{pid}` | `usePolicies.ts:107` | Remove session policy |
| GET | `/v1/sessions/{id}/resources/terminals?order=asc&limit=1000` | `useTerminals.ts:262,304` | List terminals |
| POST | `/v1/sessions/{id}/resources/terminals` | `NewTerminalButton` | Create terminal |
| GET | `/v1/sessions/{id}/resources/files` | `filesApi.ts:17` | File upload/list |
| GET | `/v1/sessions/{id}/resources/environments/default` | `useWorkspaceChangedFiles.ts:596` | Env availability (Files tab gate) |
| GET | `…/environments/default/changes` | `useWorkspaceChangedFiles.ts:166` | Changed-files list |
| GET | `…/environments/default/filesystem[/{path}]?limit=1000&order=asc` | `useWorkspaceChangedFiles.ts:284,403,521` | Browse workspace files |
| GET | `…/environments/default/search?…` | `useWorkspaceChangedFiles.ts:349` | Search workspace |
| GET/PUT/PATCH/DELETE | `/v1/sessions/{id}/codex_goal` | `codexGoalApi.ts:152,178,216` | Codex goal read/set/update/clear |
| GET | `/v1/sessions/{id}/codex_goal/status` | `codexGoalApi.ts:199` | Codex goal status |

### REST — fleet / hosts / policies / capabilities

| Method | Path | File:line | Trigger |
|---|---|---|---|
| GET | `/v1/agents[?after=]` | `useAvailableAgents.ts:120` | Built-in agent catalog (picker) |
| GET | `/v1/runners` | `sessionsApi.ts:689` | Online runners (bind-only-online) |
| POST | `/v1/hosts/{hostId}/runners` | `sessionsApi.ts:593` | Launch runner / fork-resume bind |
| GET | `/v1/hosts/{hostId}/filesystem[/{path}]` | `useHostFilesystem.ts:54` | New-chat host file browser |
| POST | `/v1/hosts/{hostId}/directories` | `useHostFilesystem.ts:203` | Create dir on host |
| GET | `/v1/policy-registry` | `usePolicies.ts:42` | Policy types registry |
| GET | `/v1/policies` | `useDefaultPolicies.ts:23` | Default (server-level) policies |
| POST | `/v1/policies` | `useDefaultPolicies.ts:50` | Create default policy |
| PATCH | `/v1/policies/{id}` | `useDefaultPolicies.ts:64` | Toggle default policy |
| GET | `/v1/info` | `capabilities.ts:104` (via `hostFetch`) | Boot capabilities probe |
| GET | `/v1/me` | `identity.ts:64` (via `hostFetch`) | Identity probe (header mode) |
| GET | `/health?session_ids=<csv>` | `useRunnerHealth.ts:95` | Open-session liveness poll (~10 s) |

### REST — accounts/auth (only when `accounts_enabled`, bare `fetch`, cookie)

| Method | Path | File:line | Trigger |
|---|---|---|---|
| POST | `/auth/login` | `accountsApi.ts:63` | LoginPage |
| POST | `/auth/logout` | `accountsApi.ts:110` | AccountMenu |
| GET | `/auth/me` | `accountsApi.ts:131` | Accounts identity |
| POST | `/auth/register` | `accountsApi.ts:157` | Redeem invite |
| POST | `/auth/users/me/password` | `accountsApi.ts:210` | Self password change |
| POST | `/auth/setup` | `accountsApi.ts:251` | First-run admin claim |
| GET | `/auth/users` | `accountsApi.ts:367` | MembersPage (admin) |
| POST | `/auth/invite` | `accountsApi.ts:386` | Mint single-use invite (admin) |
| POST | `/auth/users/{id}/reset` | `accountsApi.ts:~395` | Reset password (admin) |
| DELETE | `/auth/users/{id}` | `accountsApi.ts:418` | Delete user (admin, cascades) |
| (GET) | `/api/version` | (`/v1/info` server_version mirror) | version footer |

### WebSocket / SSE channels

| Channel | Path | File:line | Notes |
|---|---|---|---|
| **SSE** | `GET /v1/sessions/{id}/stream[?idle=true]` | `sessionsApi.ts:921` | Open-chat live-tail; `response.*` + `session.*`; presence uplink; `[DONE]` sentinel; reconnect on drop |
| **WS** | `/v1/sessions/updates` | `sessionUpdatesSocket.ts:66` | Sidebar live feed; watch-set ↔ snapshot/changed/removed/heartbeat |
| **WS** | `/v1/sessions/{id}/resources/terminals/{tid}/attach[?read_only=true]` | `TerminalView.tsx:417,440` | xterm.js ↔ runner tmux; read-only for viewers |

**Note on user search:** `GET /v1/users/search` is **not** a direct SPA call — the
permissions "add user" combobox uses a host-injected `searchUsers` callback
(`hooks/useUserSearch.ts:25,39`; `host.ts:43`) and is inert (plain text input) in
standalone. The `/v1/users/search` route in `CUJ-ANALYSIS §2.E` is what an
embedding host's callback hits.

---

## Q3 — Streaming ↔ durable reconciliation (the merge point)

The renderer walks one flat `blocks: AnyBlock[]` fed from (a) durable snapshot
items (`GET …/items` → `itemsToBlocks`, each block has `ctx.itemId`) and (b) live
SSE (`parseSseStream` → `BlockStream.reduce`; token/reasoning blocks are id-less,
persisted items carry `ctx.itemId`). **They are merged by deduping on
`ctx.itemId`** so each persisted item renders once
(`chatStore.ts:15`; enforced at `:1455,:1904-1907,:2460-2463,:2961-2966,:3004-3008`).

**`lib/blockStream.ts` consuming SSE:** `BlockStream.reduce(events)` is a pure,
stateful reducer (hand-port of Python `_stream.py`) — text/reasoning flush
thresholds (30 chars / newline), per-response dedup sets `seenCallIds` /
`seenResultCallIds` (cleared each `response.created`), reasoning↔text closure on
tool calls and terminals. The claude-sdk MCP path emits each tool
call/result **twice** (inline observed + post-stream action_required / completed
flush); the dedup keeps one (`blockStream.ts:495-531,559-604`). It explicitly
ignores `session.*` events (no-ops, `:896-908`) — those are tapped separately.

**`pendingUserMessages` held until `session.input.consumed`:** `send()` pushes a
`PendingUserMessage{tempId:"pend_N"}` **before** the POST (`chatStore.ts:791-821`)
so the bubble paints instantly and a consumed event racing ahead of the POST
still finds it. On `session.input.consumed` (`:3724`), the pending bubble is
**promoted into `blocks`** as a committed user block carrying the server
`item_id`, matched in precision order: (1) by `clearedPendingId`, (2) FIFO head
(no text match — native transcripts reformat text), (3) fresh from payload. The
promoted block **reuses the `tempId` as React key** → no remount/flink
(`:3744-3801`). `is_meta` consumed events ignored; `hasCommittedItem` guards
double-promotion.

**Persisted items deduped by `ctx.itemId`:** in the pump, every block with a
`ctx.itemId` is skipped if already committed or buffered (`:3004-3008`), and the
`flush` re-checks at commit time (a snapshot merge may have inserted it while it
sat in the buffer, `:2961-2966`). A persisted assistant `message` whose text
already streamed id-less gets its item id **stamped onto the existing streamed
`text_done` in place** (match by `responseId`+`fullText`, FIFO) rather than
appended (`:3027-3069`) — keeping one copy in its streamed position AND letting
reconnect (item-id-keyed) see it as already rendered. This is the durable-vs-
streaming merge.

---

## Q4 — How "working/idle" is derived in the client

Two distinct notions:

**Sidebar row badge** (`useSessionState.ts:21`): from the **row's**
`status` (`idle|running|failed`) + `pending_elicitations_count`, both delivered
by the WS updates frames. Priority **awaiting > running > none** (`failed` not a
sidebar state). The store also patches the active row in cache on each
`session.status` SSE event so the dot flips in lockstep with the chat
(`patchConversationStatusInCache`, `chatStore.ts:3694`).

**Chat-surface "Working…" indicator** (`chatStore.ts:sessionStatus`,
`idle|launching|running|waiting|failed`): driven by `session.status` SSE events
(`handleSessionEvent` case `:3585`). Adds `waiting` (parent loop parked on the
async-work / sub-agent drain) which the row badge can't represent. Seeded from
the snapshot on bind so a refresh on a running session shows "Working…"
immediately. `background_task_count` is **sticky** (a Stop-hook `0` clears it; a
bare PTY `idle` leaves it untouched; a new turn/failure clears it) — so a
finished-with-background-shells claude-native turn reads "N background tasks still
running" (`:3601-3618`). The `activeResponse` sidecar tracks the per-turn
lifecycle (streaming/completed/failed/cancelled), reconciled by `session.status`,
`response_end`, and `session.interrupted`.

Updated via the **WS updates stream** (sidebar) and the **SSE stream** (chat) —
the chat's `session.status` events are authoritative for the open session.

---

## Q5 — Close-the-page-and-return behavior

**Server-durable.** Closing the tab stops nothing server-side; the turn keeps
running. On return/refresh:
1. `switchTo(id)` → `bindStream` opens a fresh `SSE …/stream` **first**, then
   fetches `getSessionSlim` + `fetchInitialHistoryWindow` concurrently and merges
   deduping by item id (`chatStore.ts:1825-1907`) — the documented
   stream-then-snapshot reconnect contract.
2. History hydrates only the most recent window (20, extended back to the prior
   user prompt — `fetchInitialHistoryWindow`, `sessionsApi.ts:833`); scroll-up
   pages older.
3. The SSE pump (`startStreamPump`, `:2542`) reconnects automatically on transport
   drops (Databricks Apps' ~5-min HTTP/2 cap): a drop after a healthy connection
   reconnects **instantly** (`failedOpens=0`); only consecutive failed opens back
   off. On each reconnect (not first connect), `reconcileOnReconnect` (`:2394`)
   pages items backward until the window overlaps the pre-gap transcript, splices
   unseen items at the right position, recovers `sessionStatus`/`activeResponse`
   (so a gap-completed turn doesn't strand the spinner), and reconciles
   ApprovalCards against `pending_elicitations`.
4. 401/403/404 on stream open → mark session failed and stop (`:2600`).

**Host offline → `ReconnectSessionDialog`** (`ChatPage.tsx:1095-1105,:952`):
liveness from `useSessionLiveness` (`hooks/useSessionLiveness.ts:187`) using
`/health`'s `runner_online`+`host_online`. The two unreachable variants open the
dialog:
- `host_offline` (host-bound, host tunnel down, host NOT web-resumable):
  shows the **CLI reconnect command** (`omnigent host --server <url>`); owner
  reconnects from that machine, any viewer can fork.
- `local_stranded` (not host-bound, runner down): shows
  `omnigent <run|claude> --resume <id> --server <url>`; restart from own machine,
  fork is the escape hatch.

Resumable managed hosts instead read `host_asleep` (composer open; next message
wakes the sandbox via server `resume_managed_host`) or `starting` while waking —
**no dialog**. A fresh session within `STARTING_GRACE_S=45` reads `starting`
("Connecting…") rather than flashing a banner during cold boot.

Background-tab nuance: a throttled tab can miss `elicitation_resolved`; on
`visibilitychange→visible` the store reconciles pending elicitations against a
fresh snapshot (`bindStream` `:1800-1808`).

---

## Q6 — TUI vs WebUI state differences (high level)

> The TUI agent owns TUI depth; this is the web client's view of the contrast.

- **Transport.** Web uses HTTP SSE (`…/stream`) + WS (updates, terminal-attach)
  through `authenticatedFetch`/`resolveWebSocketUrl`; the TUI/REPL
  (`omnigent/repl/`) is a Python client driving the same server but renders to a
  terminal (rich streaming, slash-command menu, resume picker, event tape). Both
  consume the **same `response.*`/`session.*` event vocabulary** — the web client
  even hand-ports the Python `_sse.py`/`_stream.py` reducers (`sse.ts`,
  `blockStream.ts` headers) to stay byte-parity with the TUI/SDK client.
- **Durability.** Web messages on **non-native** sessions are persisted at POST
  time (synchronous `item_id` in the POST response, then `session.input.consumed`
  promotes the optimistic bubble). On **native-terminal** sessions
  (claude-native/codex-native), a web message is NOT persisted at POST — it
  round-trips through the vendor TUI and reconciles via the forwarder's
  `session.input.consumed`, which can arrive after a transient idle/failed; the
  web client special-cases this (skip idle-clear, replay `pending_inputs` on
  rebind — `chatStore.ts:3653-3667,:1957-1960`). A TUI session typed directly in
  the vendor binary has no optimistic web bubble at all; the web client renders
  those as fresh committed bubbles (consumed-event path #3).
- **Working state.** Web derives a sidebar badge (awaiting>running) + a chat
  indicator (idle/launching/running/waiting/failed) from `session.status` events.
  The TUI shows its own inline spinner driven by the same status stream.
- **Presence / collaboration.** Web has presence avatars, share/permissions,
  comments, and a subagents rail — multi-viewer concerns that the single-user TUI
  lacks. Presence is the SSE stream URL itself (holding `…/stream` open = a
  viewer; `?idle=` flips presence-idle).
- **Model/effort.** Web exposes a model/effort picker for SDK + claude-/codex-
  native (and injects `/model` into the tmux pane for claude-native); for vendor-
  owned-model harnesses (qwen/goose/cursor/pi/opencode) the web hides the label
  and defers to the TUI's own picker (`nativeVendorOwnsModel`,
  `chatStore.ts:287-296`).

---

## Per-harness notes (web client's branches)

- **claude-sdk / polly (on claude-sdk):** standard persisted-at-POST flow; the
  reducer's MCP double-event dedup (`blockStream.ts:495-531,559-604`) is the only
  harness-specific path. *Live traces available in corpus
  (`conv_b4f2…`, `conv_6354…`).*
- **claude-native:** `isNativeTerminalSession=true`; not persisted at POST;
  `session.todos`→TodoPanel; `slash_command` round-trips tmux and pops FIFO
  pending with no consumed event (`:3804`); Stop hard-kills the tmux pane;
  `setModel` injects `/model` into the pane. *Live trace `conv_d0dd…`.*
- **codex / codex-native:** no live trace (creds expired). codex-native: web
  treats like any native-terminal session; surfaces `codexCommand`/exec
  elicitations + a Plan-mode toggle (`codexPlanMode`); has a `codex_goal`
  REST surface. Structurally analogous to claude-native on the web side.

---

## Failure branches & gaps

- No `[DONE]` → reconnect (instant if healthy); 401/403/404 → fail+stop
  (`:2600`); reader/parse error (`ERR_HTTP2_PROTOCOL_ERROR`) → drop→reconnect
  (`:3151`).
- Policy-denied input: POST `{denied:true}`, no consumed event → settle from POST
  response, roll back bubble (`:898-918`); also a `policy_denied`/`response.policy_denied`
  block.
- WS updates half-open socket → 70 s watchdog reconnect; 45 s HTTP fallback while
  down.
- Search-index reindex race: rename/delete patch cache in place, never immediate
  refetch (`useConversations.ts:291-445`).
- "Stuck-pending bubble" class: `switchTo` stashes only unsettled own sends
  (`pend_`, `posted!==true`); settled sends defer to server `pending_inputs`;
  content-equal snapshot twin drops the stash copy (`:1179-1217,:1932-1960`).
- **Per `CUJ-ANALYSIS §2.E` (unverified by me):** CJK IME Enter submits
  prematurely (#433); non-git Changes panel empty (#725); browser empty after
  reconnect (#386); mobile HTML preview/download (#968/#969). *(per doc — unverified)*

## Open questions

- Exact wire parity of `SessionListWireItem` (WS frames) vs. `GET /v1/sessions`
  rows (client assumes it via `nullsToUndefined`) — server SME.
- Whether the WS updates push of `runner_online`/`host_online` makes the
  `/health` poll redundant; code comments say the host control plane doesn't push
  runner-reaped state, so the poll stays load-bearing (`useRunnerHealth.ts:22-23`).
- No live `omni-web` traces (telemetry opt-in); validating click→server
  `traceparent` continuation would need `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` set.
