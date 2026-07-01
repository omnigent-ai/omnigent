> **Component architecture doc** — part of the Omnigent master architecture. Overall arch + diagrams: [../ARCHITECTURE.md](../ARCHITECTURE.md). **Round-2 live-driving corrections** (timers, runner failover, switch-agent, add-policy gate, …): [../ARCHITECTURE.md §10](../ARCHITECTURE.md). Also embedded as a §7 subsection of the master doc.

# Component: WEB UI (React SPA under `web/src/`)

All anchors verified against `<WT>=/home/dhruv.gupta/oss/omnigent-worktrees/master-arch-docs`.
Coverage: static analysis of `web/src` + the server-side endpoint traces (`webui-endpoints` corpus
row, conv `conv_32db…`). No live browser; the browser-origin `omni-web` OTel span is out of scope.

## 1. Role & boundaries

The WebUI is a **pure SPA client of `omni-server`'s `/v1` REST + SSE + WS surface.** It never talks
to a runner, harness, or host directly — every byte goes through one choke point (`lib/host.ts:138`
`hostFetch`: standalone → `fetch`; embedded → host-injected fetcher) and `resolveWebSocketUrl`
(`host.ts:143`) for WS. It owns: the optimistic-send UX, the **streaming↔durable reconciliation**
(`lib/blockStream.ts` + `store/chatStore.ts`), the sidebar/projects/inbox caches (TanStack Query),
and client-derived "Working…" state. It does NOT own conversation history (server is source of
truth), turn execution, policy evaluation, or any persistence — it re-derives a view and POSTs
intents. Two render modes share all of the above: **standalone** (served by omni-server or Vite
proxy) and **embedded** (a host supplies `fetcher`/`resolveWebSocketUrl`/`searchUsers` via
`OmnigentHostConfig`, `lib/host.ts`).

## 2. Key files & entrypoints (verified path:line)

- `store/chatStore.ts` — Zustand module-scope store; the heart. `send` `:777`, `sendSlashCommand`
  `:995`, `stop` `:1124`, `switchTo` `:1171`, `submitApproval` `:1283`, `bindStream` `:1783`,
  `startStreamPump` (reconnect loop) `:2542`, `pumpStreamEvents` `:2900`, `reconcileOnReconnect`
  `:2394`, `handleSessionEvent` (the `session.*` SSE side-effect switch) `:3412`,
  `patchConversationStatusInCache` `:3979`. Lives outside React so the SSE stream survives remounts.
- `lib/blockStream.ts` — hand-port of `sdks/python-client/omnigent_client/_stream.py`. The reducer
  state machine; **dedup-by-itemId is at the store layer, not here** (this file is a pure block
  factory). `processEvent` `:342`, `closeText`/`closeReasoning` `:251`/`:217`, tool-call dedup
  `:495`, `message_done` race-dedup `:682`. Class `BlockStream.reduce` `:932`.
- `lib/sse.ts` — SSE byte-stream parser (`parseSseStream` `:102`, getReader not for-await, iOS
  Safari < 17.4 bug) + `parseEvent` `:269` (the **authoritative event taxonomy**, ~40 types).
- `lib/sessionsApi.ts` — typed `/v1/sessions` client (REST + the SSE open). `createSession` `:394`,
  `forkSession` `:495`, `switchSessionAgent` `:545`, `launchRunner` `:576`, `updateSession`(PATCH)
  `:629`, `getSessionSlim` `:746`, `fetchSessionItemsPage` `:782`, `fetchInitialHistoryWindow`
  `:833`, `postEvent` `:883`, `openSessionStream` `:921`, `interrupt` `:939`, `stopSession` `:950`,
  `approve` `:967`.
- `lib/sessionUpdatesSocket.ts` — singleton WS client for `WS /v1/sessions/updates` (sidebar push).
  Frame types `:23`, watchdog `HEARTBEAT_WATCHDOG_MS=70_000` `:48`, reconnect backoff `:50`.
- `hooks/SessionUpdatesProvider.tsx` — wires WS frames into the `["conversations"]` /
  `["project-sessions"]` query caches; derives the watch-set `:159`.
- `hooks/useConversations.ts` — sidebar infinite query (`fetchConversationsPage` `:178`, 20/page,
  `order=desc sort_by=updated_at`), project hooks, all rename/delete/archive mutations.
- `hooks/useSessionState.ts` — **sidebar-row badge ONLY** (`getSessionState` `:21`); awaiting >
  running > none. (NOT the chat "Working…" — see §6.)
- `hooks/useSessionLiveness.ts` — open-session liveness truth table (`useSessionLiveness` `:187`).
- `hooks/useRunnerHealth.ts` — socket-down fallback poll of `GET /health?session_ids=` `:95`.
- `lib/capabilities.ts` (`resolveServerInfo` → `GET /v1/info` `:96`) + `lib/CapabilitiesContext.tsx`.
- `lib/identity.ts` — `resolveIdentity` → `GET /v1/me` `:59`; `authenticatedFetch` `:126`
  (injects `X-Forwarded-Email`, `cache:"no-store"`, 401→login redirect).
- ChatPage working-state helpers: `computeIsWorking` `pages/ChatPage.tsx:4684`, `computeShowsWorking`
  `:4705`, `shouldShowWorkingIndicator` `:2346`.

## 3. Internal model (chatStore)

The store splits **reactive render state** from **internal bookkeeping** (`ChatState`,
`chatStore.ts:198`). Core:

- `blocks: AnyBlock[]` (`:222`) — the flat list the renderer walks. Holds committed history
  (hydrated by `itemsToBlocks`) **and** streaming output appended at the tail. **Single dedup key:
  `block.ctx.itemId`.**
- `pendingUserMessages: PendingUserMessage[]` (`:223`) — optimistic user bubbles POSTed but not yet
  acked by `session.input.consumed`. Held OFF `blocks` so a prior turn's streaming output appends
  cleanly. Each has a client `tempId` (`pend_<n>`, NOT the server id — keeps React key stable across
  the POST), a `posted` flag, and an `author`.
- `pendingByConversation: Record<id, StashedPending>` (`:246`) — per-conversation stash of **only
  this client's UNSETTLED (`posted!==true`) own bubbles**, so an in-flight send survives in-app
  navigation. A baseline `committedTexts` (`StashedPending`, `:150`) prevents the "disappears then
  reappears" dedup bug on resumed sessions with prior history.
- `activeResponse: {responseId, state, error}` (`:248`) — lifecycle of the in-flight turn.
- `status: "idle"|"streaming"` (`:258`, UI-local send-in-flight) vs `sessionStatus: SessionStatus`
  (`:273`, server-authoritative `idle|launching|running|waiting|failed`, seeded on bind, driven by
  `session.status` SSE). These are **distinct** — `sessionStatus` adds `waiting` (parent parked on
  async-work drain) which `status` can't represent.
- `isNativeTerminalSession` (`:286`) — derived from the `omnigent.wrapper` label on bind; gates the
  whole optimistic-bubble lifecycle (native messages reconcile via transcript round-trip, can arrive
  after a transient idle).
- History window: `hasMoreHistory`/`oldestItemId`/`historyGeneration` (`:355`,`:363`,`:493`) — bind
  hydrates one windowed page; scroll-up `loadMoreHistory` pages older. `historyGeneration` is a
  monotonic guard that voids in-flight page reads after a window reset.
- Plus ~25 snapshot-hydrated session fields (model/effort/cost/usage/todos/skills/viewers/sandbox/
  terminalPending), each updated by a matching `session.*` SSE event.

Module-scope singletons: `sendChain` (`:581`, serializes POSTs in submit order),
`pendingInitialPrompts` map (`:694`, NewChatDialog→ChatPage first-message handoff).

## 4. Inter-component channels (every edge in/out)

The WebUI's only peer is **omni-server**. Three transports:

```
 ┌─────────┐  REST /v1/* (hostFetch, X-Forwarded-Email)          ┌────────────┐
 │ WebUI   │ ───────────────────────────────────────────────────▶│            │
 │ (SPA)   │  SSE  GET /v1/sessions/{id}/stream  (per-conv tail)  │ omni-server│
 │         │ ◀═══════════════════════════════════════════════════│            │
 │         │  WS   /v1/sessions/updates  (sidebar push, full-state)│            │
 │         │ ◀────────────────────────────────────────────────▶  │            │
 │         │  WS   /v1/sessions/{id}/resources/terminals/{t}/attach│            │
 │         │ ◀────────────────────────────────────────────────▶  └────────────┘
 └─────────┘   (terminal xterm bytes; TerminalView.tsx:443)
```

The runner/harness/host edges (`POST /v1/sessions`, `/agent/contents`, `/skills`,
`/policies/evaluate`, the `/v1/runners/{id}/tunnel` WS) seen in the corpus are **server↔runner**, not
client-facing — the WebUI never sees them. Trace evidence (`summary_conv_32db…`): the
`webui-endpoints` GET battery hit `GET /v1/sessions/{id}` ×11, `/items` ×5, plus
`/agent`, `/policies`, `/skills` — all received by `omni-server` (the runner-bound work rides the
tunnel WS, `tree_conv_32db…` trace 4/19).

### Durable vs streaming per channel

| Channel | Direction | Carries | Durable? |
|---|---|---|---|
| `SSE /v1/sessions/{id}/stream` | server→client | `response.*` (task-scoped: text/reasoning/tool deltas, lifecycle) **+** `session.*` (session-scoped: status/usage/presence/resource/elicitation) | **streaming** (no replay buffer); `[DONE]` sentinel = clean close |
| `WS /v1/sessions/updates` | bidir | client `{type:"watch", session_ids}`; server `snapshot`/`changed`/`removed`/`heartbeat` (full-row, never field deltas) | sidebar list freshness |
| `WS …/terminals/{t}/attach` | bidir | raw PTY bytes (xterm.js ⇄ tmux) | live only |
| `GET /v1/sessions/{id}/items` | client→server | committed conversation items (paginated, `order=desc`) | **durable** (source of truth) |
| `POST /v1/sessions/{id}/events` | client→server | intents: `message`/`slash_command`/`interrupt`/`approval`/`stop_session`/`compact` | item-typed persisted before 202 returns |

### The COMPLETE set of API requests the WebUI sends (exhaustive, from hooks/stores)

**Boot / identity / capabilities**
- `GET /v1/info` — `lib/capabilities.ts:104` (accounts/sandbox/databricks/version gates).
- `GET /v1/me` — `lib/identity.ts:64` (current user; 401→`login_url` redirect).
- `GET /api/version` — `lib/host.ts` / info popover.
- `POST /auth/login`, `/auth/logout`, `/auth/setup`, `/auth/register`, `/auth/invite`,
  `/auth/magic/redeem`, `GET /auth/me`, `/auth/users…/password|reset` — accounts mode
  (`lib/accountsApi.ts`, LoginPage/RegisterPage/MembersPage). Only reachable when `accounts_enabled`.

**Sidebar / projects / discovery**
- `GET /v1/sessions?order=desc&sort_by=updated_at&limit=20[&after=][&search_query=][&include_archived=][&project=]`
  — `useConversations.ts:201` (infinite), `fetchAllProjectSessionIds` `:727`, `fetchProjectSessionIds`
  `:753`, `useAgents.fetchAgents` (`?limit=100` `:82`).
- `GET /v1/sessions/{id}` — `fetchConversationById` (pinned backfill) `:151`.
- `GET /v1/sessions/projects` — `useProjects` `:665`.
- `WS /v1/sessions/updates` — `sessionUpdatesSocket.ts:66` (sole sidebar live channel).

**Open a conversation (bind)** — see §7 ordering
- `GET /v1/sessions/{id}?include_items=false&include_liveness=false[&refresh_state=true]` —
  `getSessionSlim` `:746`. (Slim! NOT the full `getSession`.)
- `GET /v1/sessions/{id}/items?limit=20&order=desc[&after=]` — `fetchSessionItemsPage` `:782`
  (also `useSessionItems.ts:66`).
- `GET /v1/sessions/{id}/stream[?idle=true]` — `openSessionStream` `:921` (SSE; holding it open
  registers presence; `?idle` is the entire presence uplink).

**Send / control (POST /v1/sessions/{id}/events)** — `postEvent` `:883`
- `{type:"message"}` (send), `{type:"slash_command", data:{kind:"skill"…}}` (skill),
  `{type:"interrupt"}` (`interrupt` `:939`), `{type:"stop_session"}` (`stopSession` `:950`),
  `{type:"compact"}` (`chatStore.compact`), `{type:"approval"}` (legacy approval path).
- `POST /v1/sessions/{id}/resources/files` (multipart) — attachment upload (`filesApi.ts:16`).

**Session lifecycle / mutation**
- `POST /v1/sessions` (JSON or multipart bundle) — `createSession` `:394` / `createBundledSession`
  `:441`.
- `POST /v1/sessions/{id}/fork` — `forkSession` `:495`.
- `POST /v1/sessions/{id}/switch-agent` — `switchSessionAgent` `:545`.
- `POST /v1/hosts/{hostId}/runners` — `launchRunner` `:576` (fork-resume bind).
- `PATCH /v1/sessions/{id}` — `updateSession` `:629` (model/effort/cost/collab/runner/silent),
  `renameConversation` `:249` (title), `archiveConversation` `:266`, `moveConversationToProject`
  `:673` (project label).
- `DELETE /v1/sessions/{id}[?delete_branch=true]` — `deleteConversation` `:283`.
- `GET /v1/runners` — `listRunners` `:688` / `bindOnlyOnlineRunner` `:698`.

**Approvals / policies / permissions / comments**
- `POST /v1/sessions/{id}/elicitations/{eid}/resolve` — `approve` `:967` (primary approval path;
  ApprovePage deep-link uses the same).
- `GET /v1/policy-registry` `usePolicies.ts:42`; `GET/POST /v1/sessions/{id}/policies` `:35`/`:83`;
  `DELETE …/policies/{pid}` `:107`; `GET/POST/DELETE /v1/policies[/{id}]` (admin PoliciesPage).
- `GET/POST/DELETE /v1/sessions/{id}/permissions[/{userId}]`, `GET …/owner` —
  `lib/permissionsApi.ts:96-130`.
- `GET/POST/DELETE/PATCH /v1/sessions/{id}/comments[/{commentId}]`, `POST …/comments/send` —
  `useComments.ts:47-164` (Inbox `useCommentInbox` reuses).
- `PATCH /v1/sessions/{id}/read-state` — unread tracking.

**Files / agent introspection / codex / health**
- `GET /v1/sessions/{id}/resources/environments/{env}/filesystem/{path}` — file content
  (`useFileContent.ts`); `…/diff/{path}` — `useFileDiff.ts`; changed-files + dir listing.
- `GET /v1/hosts/{id}/filesystem`, `…/directories` — new-chat workspace browser (`useHostFilesystem.ts:127,203`).
- `GET /v1/sessions/{id}/agent` (+ `/agent/mcp-servers[/…]`) — `useAgents.ts:134,196`.
- `GET/PUT /v1/sessions/{id}/codex_goal[/status]` — `lib/codexGoalApi.ts:152,178,199`.
- `GET /health?session_ids=…` — `useRunnerHealth.ts:95` (socket-down liveness fallback).
- `WS /v1/sessions/{id}/resources/terminals/{tid}/attach[?read_only=true]` — `TerminalView.tsx:443`.
- `POST <otel>/v1/traces` — browser telemetry (`lib/telemetry.ts:44`, the out-of-scope `omni-web`
  span source).

**NOT a client REST call:** user search is host-IoC (`getOmnigentUserSearch`, `useUserSearch.ts`) —
the SPA never calls a `/v1/users/search` endpoint itself (correction to CUJ-ANALYSIS).

## 5. CUJ behaviors

### Send → optimistic bubble → durable promotion (`send` `:777`)
1. Push a `pend_<n>` bubble to `pendingUserMessages` BEFORE the POST (renders instantly).
2. `ensureBoundSession` (`:1539`): for a brand-new session → `createSession` + `bindStream` +
   `opts.onConversationCreated` (navigate `/`→`/c/:id`) then POST; for an existing session whose
   stream died → rebind first so response events have a subscriber.
3. Upload files (real `file_id`s), then `postEvent {type:message}`. Serialized through `sendChain`
   so rapid sends reach the server in order.
4. On 202: mark bubble `posted:true`, drop its stash copy. On `denied:true`: roll back the bubble
   from the POST response (no `session.input.consumed` will ever come). On throw: roll back + append
   a client error block (or mark the active response failed).
5. The bubble clears when `session.input.consumed` promotes it to a committed `blocks` entry
   (`handleSessionEvent` `:3724`), matched: (1) by `clearedPendingId`, (2) FIFO head, (3) fresh
   render — keeping the same React key (`stableKey=tempId`, no remount).

### Streaming↔durable reconciliation (the core Q) — `pumpStreamEvents` `:2900`
- The pump taps the raw SSE for `session.*` side effects (`tapSessionEvents`→`handleSessionEvent`)
  and live-delta previews (`tapLiveDeltas`, claude-native) BEFORE handing the rest to
  `BlockStream.reduce`.
- **Dedup is enforced in three places, all keyed by `ctx.itemId`:** (a) at emit time the pump skips
  any itemId already in `blocks` or in the rAF buffer (`:3004`); (b) at flush commit-time it
  re-checks itemIds because a snapshot merge can race a buffered block in (`:2961`); (c) `bindStream`
  filters snapshot blocks against `state.blocks`' itemIds (`:1907`). Elicitations dedup by
  `elicitationId` instead (not persisted items, `:3018`).
- **The streaming↔persisted merge point:** a streamed assistant `text_done` is initially id-less; the
  relay later re-publishes it as `output_item.done` carrying the store id. The pump stamps that id
  **onto the already-rendered streamed block in place** (`:3027`) rather than appending — so the live
  view keeps one copy in its streamed position and reconnect (itemId-keyed) sees it as rendered.
- Rendering is rAF-coalesced (`createRafScheduler` `:2667`); first content of each response paints
  synchronously (`paintedFirstContent` `:2939`), the rest batch.

### Working vs idle (the Q) — see §6.

### Close page & return — see §7.

### Stop / interrupt (`stop` `:1124`)
Fire-and-forget `POST {type:interrupt}`; the local SSE stream stays open. Server emits
`session.interrupted` (→ `interruptedResponseIds`, marks bubble cancelled) + `response.incomplete`.
Optimistically patches the sidebar row idle (⚠️ unbacked write `:1160` — a poll mid-turn can briefly
revert the dot; self-corrects on the real idle).

### Fork / switch-agent
Fork: `POST …/fork` → new unbound session; ForkSessionDialog then `launchRunner` to bind. Switch:
`POST …/switch-agent` keeps the same session; the `session.agent_changed` SSE re-derives
`isNativeTerminalSession` via `refreshSessionBinding` (`:3517`) since the URL doesn't change.

### ⚠️ Failure branches I can confirm in code
- Stream open 401/403/404 → give up, `sessionStatus:"failed"`, no infinite spinner (`:2600`).
- `POST /events` 503 (runner never came online) → typed `ApiError.code`, standalone error block
  appended so the user sees WHY (`:982`).
- Background-tab throttling can drop `elicitation_resolved`; a `visibilitychange` listener
  reconciles pending cards against a fresh snapshot on re-show (`bindStream` `:1800`).
- `session.superseded` (Claude `/clear`) drops the superseded conv's pending bubbles and redirects
  via `redirectToConversationId` (`:3848`) — else the `/clear` bubble spins forever.

## 6. Answers to the doc questions

**How "working vs idle" is derived in the client & whether it agrees with the server.**
TWO independent derivations:
- **Chat surface "Working…":** `computeIsWorking(sessionStatus)` = `sessionStatus∈{running,waiting}`
  (`ChatPage.tsx:4684`); the display gate `computeShowsWorking` (`:4705`) additionally OR-s
  `backgroundTaskCount>0` and suppresses on `runnerOnline===false` / pending elicitation.
  `sessionStatus` is **server-authoritative** — seeded from the snapshot on bind and updated only by
  `session.status` SSE events (`handleSessionEvent` `:3585`). So the chat agrees with the server by
  construction. The local `status` flag is a separate "is a send in flight" latch.
- **Sidebar row badge:** `getSessionState` (`useSessionState.ts:21`) reads the **list row's**
  `status` + `pending_elicitations_count` (awaiting > running > none) — these come from
  `GET /v1/sessions` / the WS updates stream, NOT the chat store. The store's
  `patchConversationStatusInCache` (`:3979`) mirrors the chat's live `session.status` into the active
  row so the dot doesn't lag a poll behind the chat indicator (it mirrors the server's own
  running/waiting→"running" collapse, so it never fights the poller).
  **(CUJ-ANALYSIS conflates these — see §8.)**

**How the WebUI reconciles streaming vs durable into one coherent view.** See §5. One `blocks` list;
dedup-by-`ctx.itemId` at three layers; `pendingUserMessages` held off `blocks` until
`session.input.consumed`; the streamed↔persisted text merge stamps the durable id onto the streamed
block in place.

**The ENTIRE set of API requests.** Enumerated exhaustively in §4.

**How the sidebar fetches items (pagination + live updates).** `useConversations` infinite query
(`:229`): cursor-paginated `GET /v1/sessions`, 20/page, `order=desc sort_by=updated_at`,
`getNextPageParam=last_id`. Live updates ride `WS /v1/sessions/updates` (`SessionUpdatesProvider`):
client pushes a watch-set (every cached conversation id + the open session, even off-sidebar
children, `:177`); server replies `snapshot` then `changed`/`removed`/`heartbeat` (full rows). The
provider patches matching rows in place (`mergeItemsIntoPages`) and falls back to a debounced
`invalidateQueries(["conversations"])` for structural/membership/sort changes it can't reconstruct
locally (`:249`). HTTP poll is the fallback only: `false` while the socket is connected (unless a
list opts into 60s reconcile), `45s` when disconnected (`:240`). A 70s silence watchdog
(`HEARTBEAT_WATCHDOG_MS`) force-reconnects a silently-dead socket.

**What happens when you close the page and come back.** Server is durable; the session keeps running
while the page is closed. On return, `switchTo`→`bindStream` (`:1783`): (1) open the SSE stream
FIRST (`startStreamPump`), (2) concurrently fetch the **slim** snapshot
(`getSessionSlim{refresh_state:true}`) + the initial windowed history page
(`fetchInitialHistoryWindow` — `max(1 page, back-to-previous-user-prompt)`), (3) merge snapshot
blocks into whatever the pump already pushed, deduping by itemId, and replay `pending_elicitations`
+ `pending_inputs` from the snapshot (the SSE stream has no replay buffer, so a prompt fired while
away only re-renders from the snapshot). Stream-first-then-snapshot is the documented reconnect
contract — events that arrived before the snapshot are deduped, events after are kept. A reconnect
(not first connect) additionally drops the stale in-flight bubble and runs `reconcileOnReconnect`
(`:2394`), which pages backward up to `RECONNECT_BACKFILL_MAX_PAGES` until the fetched window
overlaps the pre-gap transcript, then splices missed committed items + recovers elicitation state
the dead socket swallowed.

**TUI-vs-WebUI state differences (web side).** Both clients POST the same `/v1/sessions/{id}/events`
intents and consume the same SSE vocabulary, so transcript content converges. Web-specific state the
TUI has no analog for: the optimistic `pendingUserMessages`/stash machinery (the REPL prints
synchronously), the rAF flush scheduler, presence (`?idle` uplink via stream reconnect), the
`WS /v1/sessions/updates` sidebar push (TUI re-lists on demand), and the live-delta provisional
preview for claude-native (`live:<msgId>` blocks). The client↔server credential is
`X-Forwarded-Email` injected by `authenticatedFetch` (web) vs the REPL's own header.

## 7. Reliability gaps / sharp edges (confirmed in code)

- **`stop()` optimistic sidebar patch is unbacked** (`:1155-1160`): unlike the SSE-driven caller, no
  server event backs it, so a `useConversations` poll interleaving while the turn is genuinely still
  running can briefly revert the sidebar dot. Self-corrects on the real idle. Documented in-code.
- **SSE has no replay buffer** — every transient (`response.error`, elicitation, presence) is lost if
  the client isn't subscribed. Mitigations are snapshot-replay (`pending_elicitations`,
  `last_task_error`→synthetic error block `:2024`) and the visibility reconcile, but a transient with
  no durable equivalent (e.g. a mid-turn `response.retry`) is simply gone on reconnect.
- **Native-terminal idle race:** `session.status:idle` deliberately does NOT clear pending bubbles for
  `isNativeTerminalSession` (`:3665`) because the transcript-forwarder `session.input.consumed` can
  arrive after a transient idle; correctness here leans on the server-side `pending_inputs` TTL. If
  that event is permanently lost, the bubble relies on the next snapshot dedup to clear.
- **Two reconnect/backoff implementations** (chatStore stream pump `:2107` and
  sessionUpdatesSocket `:50`) with the same 250ms/5s/jitter constants duplicated — drift risk.
- **Databricks Apps ingress caps a single HTTP/2 stream at ~5 min** (`:589`): the SSE pump treats a
  drop-without-`[DONE]` as reconnectable and re-subscribes instantly after a healthy connection
  (`failedOpens` stays 0). A reader/parse error (`net::ERR_HTTP2_PROTOCOL_ERROR`) is also a "dropped",
  not a failure, so a routine recycle stays invisible.
- **`fetchInitialHistoryWindow` cap** (`MAX_INITIAL_PAGES=8`, `:805`): a pathological single turn
  spanning >8 pages opens with `hasMore:true` and the prompt above the response possibly not loaded
  until scroll-up — bounded, not silent truncation.

## 8. Corrections to CUJ-ANALYSIS §2.E

1. **§2.E "Working/idle state" (lines 261-262) is wrong about the source.** It says
   `hooks/useSessionState.ts` derives THE working/idle state from `status` +
   `pending_elicitations_count`. That file is **the sidebar-row badge ONLY** (`getSessionState`,
   `useSessionState.ts:21`, header comment explicitly says so). The **chat surface** "Working…"
   comes from the chat store's `sessionStatus` (driven by `session.status` SSE) via
   `computeIsWorking`/`computeShowsWorking` in `ChatPage.tsx:4684/4705` — a different field, a
   different code path, server-authoritative. The two should be documented separately.
2. **§2.E "Close page & return" (line 254) says refresh refetches `GET /sessions/{id}`.** It actually
   calls `getSessionSlim` — `GET /v1/sessions/{id}?include_items=false&include_liveness=false&refresh_state=true`
   (`sessionsApi.ts:746`), NOT the full snapshot — and crucially **opens the SSE stream FIRST**, then
   the slim snapshot + a *windowed* history page (not full history). Items come from
   `GET …/items` (paginated), not embedded in the session response.
3. **§2.E "Streaming↔durable reconciliation" (lines 258-260) locates dedup-by-itemId in
   `lib/blockStream.ts`.** `blockStream.ts` is a **pure block factory** (hand-port of `_stream.py`);
   it carries no `blocks` array and does no itemId dedup. Dedup-by-`ctx.itemId` lives in
   **`chatStore.ts`** at three sites: pump emit-time (`:3004`), flush commit-time (`:2961`), and
   `bindStream` snapshot merge (`:1907`). The streamed↔persisted *merge point* (stamping the durable
   id onto the streamed block in place) is `pumpStreamEvents:3027`.

Minor: §2.E table (line 455) labels WebUI live-in as "`WS /health/subscribe`" — the actual
socket-down liveness path is an HTTP **poll** `GET /health?session_ids=` (`useRunnerHealth.ts:95`),
not a WS subscribe; and user search is host-IoC, not a `GET /v1/users/search` the bundle issues.
