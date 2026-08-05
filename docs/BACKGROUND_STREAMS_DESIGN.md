# Background conversation streams

Refactor `chatStore` from one active conversation into a registry of
per-conversation stores, so switching conversations no longer tears down the
session stream.

## 1. Motivation

Attaching to `/c/<id>` opens `GET /v1/sessions/<id>/stream`; switching away
aborts it. `switchTo` is the "single owner of stream-bind": it aborts the prior
stream, wipes ~36 fields of state, and rebinds the new conversation.

That teardown is the source of the felt latency. On return the client pays a
stream reconnect plus a snapshot re-fetch, and anything the agent produced while
away only arrives through that rebind.

A `transcriptCache` LRU (#3932) tried to close this by painting committed blocks
instantly on switch, and was **reverted** (#4124): the window appeared, then
shifted under the reader as newer commits were gap-bridged onto it. That revert
is the strongest argument for this design. A cache can only make the switch
*paint* faster — it cannot make new messages arrive sooner, and painting a stale
snapshot before revalidating is what made the contents move. Keeping the
conversation live sidesteps both: there is nothing stale to paint and nothing to
revalidate, because the transcript never stopped being current.

The teardown is also the root of the store's most subtle code. Because state is
destroyed on switch, we need a stash for in-flight optimistic bubbles
(`pendingByConversation`) and a `committedTexts` baseline so restoring a bubble
doesn't dedupe against older history. The reverted cache added a gap-bridge
(`backfillItemsUntilCovered`, `spliceUnseenAheadOfInFlight`) on top of that.
Roughly 450 lines of the hairiest code in the file exists to work around losing
state. **Keeping conversations alive deletes that entire category of problem** —
this refactor is net-negative complexity, not just a feature.

Server-side there is no obstacle. Each open stream costs one bounded
`asyncio.Queue` (1024 events), one registry entry, and a 15s heartbeat — about
11 KB, negligible CPU. There is no per-session or per-user subscriber cap.
Holding a stream open does **not** keep the agent alive or change its lifecycle;
the runner runs regardless and `publish()` drops events when nobody is
subscribed. Those dropped events are precisely what we currently miss.

## 2. The finding that makes this tractable

The streaming machinery is **already parameterized on `(set, get)`** rather than
reaching for the store singleton:

```ts
startStreamPump(id, controller, set, get)
pumpStreamEvents(id, body, controller, set, get, scheduler)
bindStream(id, set, get, hydratePending, hydrateFromCache)
reconcileOnReconnect(id, set, get)
dropEphemeralInFlightBlocks(id, set)
applyLiveDelta(set, ...)   finalizeActive(set, ...)
```

`Setter`/`Getter` (`chatStore.ts:1913`) are structurally identical to a zustand
store's `setState`/`getState`. So if each conversation *is* a zustand store, this
entire layer transplants by passing a different `set`/`get` — no rewrite.

This is already proven in-tree: `loadtest/streamRenderBench.ts:244` drives
`pumpStreamEvents` by passing `useChatStore.setState` / `getState` straight in as
the `Setter`/`Getter`. The seam we need is one the bench harness already uses.

The exception is exactly the code that is already unsafe: `handleSessionEvent`
and its helpers call `useChatStore.setState` directly, and most branches have no
conversation guard (`session_status` at `:4400`, `session_input_consumed` at
`:4608`, `session_todos` at `:4226`, `session_usage` at `:4253`, `policy_denied`
at `:4387`). Today that is invisible because the only open stream is the active
one; with a second stream those branches would clobber the conversation you are
looking at.

So the refactor and the correctness fix are the same work, which is the main
reason to do this properly rather than bolt on a background buffer.

**Routing must be by delivering stream, not by event payload.** Several events
carry no conversation id at all — `session_input_consumed`, `session_interrupted`,
`session_resource_created`, `browser_action_request`, `retry`, `error`. You
cannot route them from their contents. A per-conversation store that owns its own
pump knows its id implicitly, which is the only routing that works.

## 3. Architecture

Three layers, in `web/src/store/`:

```
conversationStore.ts   createConversationStore(id, ctx) -> ConversationStore
conversationRegistry.ts  Map<string, ConversationStore> + capacity policy
chatStore.ts           app-global state + active-conversation projection
```

### Naming

`ConversationStore` over the suggested `ConversationFetcher`: it does not fetch,
it *owns* a conversation's state and stream lifecycle, and it literally is a
zustand store — which keeps the factory name idiomatic
(`createConversationStore`). If we would rather not lean on "store", the next
best is `LiveConversation`.

### Layer 1 — `ConversationStore`

A vanilla zustand store (`createStore` from `zustand/vanilla`), one per
conversation, holding the 36 per-conversation fields:

| Scope | Fields |
| --- | --- |
| Transcript | `blocks`, `pendingUserMessages`, `oldestItemId`, `hasMoreHistory`, `loadingMoreHistory`, `historyGeneration` |
| Turn lifecycle | `activeResponse`, `interruptedResponseIds`, `status`, `sessionStatus`, `backgroundTaskCount` |
| Binding | `boundAgentId`, `boundAgentName`, `isNativeTerminalSession`, `nativeVendorOwnsModel`, `subAgentName`, `sessionHarness`, `llmModel` |
| Session settings | `sessionModelOverride`, `costControlModeOverride`, `codexPlanMode` |
| Usage | `contextWindow`, `tokensUsed`, `sessionCostUsd`, `sessionUsageByModel` |
| Runner-owned | `todos`, `skills`, `codexModelOptions`, `terminalPending`, `gitBranch`, `sandboxStatus`, `mcpStartup` |
| Presence / load | `viewers`, `loadingConversation`, `conversationLoadError` |
| Internal | `abortController` |

It owns its stream lifecycle (`attach()` / `dispose()`), including its own
reconnect loop — see "Reconnect-on-drop must keep working per entry" below, which
is the requirement most likely to be got wrong.

It also owns the per-connection bookkeeping that is module-level singleton state
today and must become per-entry:

- `sendChain` — per-entry. Ordering only matters within a session, so this also
  removes a real cross-conversation head-of-line block: a stalled send in one
  conversation currently delays another's POST.
- `presenceAttemptController` — currently one global controller
  (`chatStore.ts:3143`). Must become per-entry so a visibility flip recycles
  *every* live stream.
- `racedNativeModelOptions`, `retiredLiveMessages`, `liveLastIndex`, the rAF
  scheduler, the `BlockStream` reducer.

`handleSessionEvent` becomes a method. Every `ChatState` write is then
structurally scoped to the right conversation, so all ad-hoc `s.conversationId
=== event.conversationId` guards get deleted and the unguarded branches are fixed
by construction. Cross-conversation side effects that are already keyed by
explicit id — react-query invalidations, `applyChildSessionUpdated`,
`useTerminalActivityStore.pulse`, `emitBrowserActionRequest` — stay as they are;
they never touch `ConversationState`.

Two callbacks keep the entry from reaching back into the root store (which would
reintroduce the coupling we are paying to remove, plus a circular import):

- `ctx.stickyPrefs` — `bindStream` reads `selectedEffort` / `selectedModel` for
  the CLI-created-session handoff.
- `ctx.queryClient`.

`session_superseded` is the one event that wants a global effect. The entry
records `supersededTo` in its own state; the root surfaces it as
`redirectToConversationId` only for the active entry. A background conversation
that gets superseded then redirects when the user switches to it, which is the
behaviour we want and comes for free.

### Layer 2 — `conversationRegistry`

Insertion-ordered `Map` keyed by conversation id, ordered by last-viewed so
iteration order is LRU. An entry is added on first navigation and never
proactively removed until the bound is hit.

**One bound: `MAX_LIVE = 30`.** An entry is either **live** (state + open SSE
stream) or **absent**. On overflow, evict the least-recently-viewed entry:
dispose it, abort its stream, drop its state. Navigating back is a cold load,
exactly as today.

There is deliberately no third "retained but detached" state. A detached entry
holding stale state that must be reconciled on return *is* `transcriptCache` in a
different shape — the very thing this refactor deletes. Reintroducing it would
mean keeping a reconcile-on-revisit path forever and answering "which of three
states is this entry in?" at every call site. Live-or-gone is the whole
simplification.

The cost is that the single bound must satisfy the connection budget, and the two
environments differ sharply:

- **Dev** — `vite.config.ts` configures `server.proxy` with no `https`/`http2`
  option, so the dev server is plaintext HTTP/1.1. Browsers cap **6 concurrent
  TCP connections per origin** there (a browser-imposed limit, not a protocol
  one; the HTTP/1.1 spec's old suggestion was 2). An SSE stream occupies one for
  its entire life, so N background streams permanently consume N of 6, shared
  with ordinary API fetches. At 5 live streams the app deadlocks: a fetch blocks
  until a stream is closed. The session-updates channel is a WebSocket, which
  after upgrade is not counted against the HTTP connection pool.
- **Production** — served behind the Databricks Apps ingress over HTTP/2, where
  all requests to an origin multiplex as concurrent *streams* over one TCP
  connection. The limit becomes the server's `SETTINGS_MAX_CONCURRENT_STREAMS`
  (commonly ~100, server-advertised), not a browser connection count.

The same ingress is already visible in the reconnect logic — `chatStore.ts:783`
documents its ~5-min cap on a single HTTP/2 stream.

So the bound is environment-derived, not a product decision:

```ts
// h2 multiplexes over one TCP connection; h1.1 is capped at ~6 per origin.
const MAX_LIVE = location.protocol === "https:" ? 30 : 3;
```

30 in production, 3 in dev until the dev server speaks HTTP/2 — because 30 live
streams on HTTP/1.1 is not merely slower, it is a hard deadlock: SSE streams
occupy all ~6 slots permanently and every ordinary API fetch blocks behind them.
Same code path, different ceiling.

That makes dev HTTPS a real follow-up rather than an optional nicety: while dev is
capped at 3, developers exercise a materially higher eviction rate than
production. Still not a blocker — behaviour is identical, only the threshold
differs.

#### Reconnect-on-drop must keep working per entry

**This is a hard requirement, not an optimization.** Every background stream needs
the same drop recovery the active stream has today, and it is load-bearing in a
way it never was before: the Databricks ingress caps a single HTTP/2 stream at
~5 minutes (`chatStore.ts:783`), so a conversation left in the background for an
hour will be dropped and re-subscribed ~12 times. An entry that fails to
reconnect goes silently stale — the user switches to it and sees a transcript
frozen at the moment they left, which is *worse* than today's cold load because
nothing signals the staleness.

The good news: `startStreamPump` (`chatStore.ts:3175`) already implements exactly
this — backoff only between consecutive failed opens, instant reconnect after a
healthy connection, 401/403 permanent, bounded transient-404 retries, and
`hasConnected` so a recovered first connect is still treated as initial. It moves
onto the entry unchanged, since it is already threaded on `(set, get)`.

**The catch: its loop conditions are `get().conversationId === id`.** There are 16
such guards in the streaming path, and they currently conflate "is this
conversation still loaded?" with "is it the one on screen?" — today the same
question. The guards must become **liveness** checks (`entry.disposed`), which
background entries pass.

Foreground is deliberately NOT the counterpart concept. Having gone through all
16: 14 are pure liveness, and about 12 of those stop being load-bearing at all
once each entry owns its state. The `flush()` bail and the `"switched"` returns
exist only because there is ONE shared `blocks` array, so a background pump would
corrupt the visible transcript; writing into your own state is always correct, so
they can be deleted rather than translated. `"switched"` itself means "stop,
someone else owns the state now" — with per-entry state, nobody does.

Getting this wrong is the single most likely way to ship a broken version of this
feature: leave the guards as-is and every background pump exits on its first
reconnect check, silently degrading to today's behaviour with all the added
complexity and none of the benefit. Worth an explicit test — *a background entry
survives a simulated drop and applies post-reconnect events.*

The guards to re-read individually are the `flush()` bail at `:3701` and the
`"switched"` returns at `:3745` / `:3922`, which exist to stop a pump writing
into a conversation the user has left —
a concern that disappears once each pump owns its own state.

**Every reconnect must reconcile, background or not.** An earlier draft here
proposed deferring `reconcileOnReconnect` for background entries to save
requests. That is wrong, and the reason is worth recording.

The ~5-minute drop is not a network failure — it is the ingress capping every
HTTP/2 stream unconditionally, on a healthy connection. It is the steady state,
~12 times an hour per stream, not an edge case. And the gap is real: the server
keeps no replay buffer and adds no sequence number, so "events emitted while no
subscriber is connected are dropped silently; reconnecting clients use the
snapshot endpoint, not replay" (`session_stream.py:81`). Reconnect is fast
(instant after a healthy connection) but not atomic, and a busy agent can emit
into that window. The subscriber queue is also bounded at 1024 events and *drains
itself* on overflow, pushing an `_OVERFLOW` marker — reachable by a throttled
background tab.

So reconciliation is **state integrity, not display preparation**. Deferring it
would leave background entries silently wrong — exactly the "frozen transcript"
failure this feature is supposed to eliminate, but harder to notice. The ~60
requests per 5 minutes is the honest cost of 30 live streams. If that proves too
expensive the levers are lowering `MAX_LIVE` or making the reconcile cheaper,
never skipping it.

Still worth doing: **stagger the reconnects.** Thirty streams opened together
recycle together, and `nextReconnectDelay` only jitters *failed* opens — a healthy
drop reconnects instantly by design. The stagger is purely herd control; it does
not reduce total work.

#### Why not just give the dev server HTTP/2?

Worth doing, and it should work — but as a parallel cleanup, not a prerequisite.

Vite 8 gets HTTP/2 for free once TLS is on: `resolveHttpServer` calls
`http2.createSecureServer({..., allowHTTP1: true})` whenever `server.https` is
set, and plain `http.createServer` otherwise. There is no separate `http2` flag —
**HTTP/2 is implied by HTTPS**, since browsers only negotiate h2 over TLS (no
browser implements h2c cleartext upgrade). So the change is `server.https` plus a
cert, e.g. `@vitejs/plugin-basic-ssl`.

Older Vite forced HTTP/1.1 whenever `server.proxy` was configured, which would
have killed this idea outright. That downgrade is gone in Vite 8:
`resolveHttpServer(app, httpsOptions)` takes no proxy argument, and the bundled
`http-proxy-3` handles HTTP/2 requests explicitly — it strips
`:method`/`:path`/`:scheme`/`:authority` pseudo-headers via an
`HTTP2_HEADER_BLACKLIST` and maps `:authority` onto `host` when forwarding
upstream.

The friction is environmental rather than technical, which is why it shouldn't
gate the refactor:

- A self-signed cert means a browser trust prompt, and every dev-server consumer
  moves to `https://` — `omnidev --vite-host 0.0.0.0 --trust-lan-origins`, the
  Electron shell, the Android screenshot task (which `adb reverse`s 5173 and
  hardcodes `http://…`), and the e2e-UI `--ui-base-url` default. Self-signed
  certs are also extra pain on Android.
- It changes the dev topology for everyone on the team to raise a limit that only
  matters above ~4 concurrent streams.

Recommended order: land the refactor with the protocol-derived cap above, then
enable dev HTTPS as a focused follow-up so dev and production converge on 30.

API: `acquire(id)` (get-or-create + attach, mark most-recently-viewed, evict LRU
if over `MAX_LIVE`), `active()`, `release(id)` (on conversation delete, replacing
`evictTranscriptCache`), `recycleAll()` (presence flip).

**Eviction must never discard unsent user work.** The one case where dropping an
entry is not equivalent to a cold load: an entry holding an unsettled optimistic
bubble (a `send` whose POST hasn't returned) or queued messages. Losing that
loses the user's message outright, since the server hasn't been told about it
yet. So an entry is **ineligible for eviction** while it has queued messages or
in-flight sends; skip to the next LRU candidate. This is the same hazard the
`pendingByConversation` stash exists to handle today — the pin replaces the
stash. If every entry is pinned (pathological), let the map exceed 30 rather than
destroy data.

**Background block growth is left unbounded, deliberately.** A live background
conversation grows `blocks` with no cap. Measured against a real corpus (166
local Claude Code sessions for this repo): p50 48 KB, p90 0.4 MB, p99 3 MB, with
a single 20 MB outlier — mean 0.30 MB. Thirty typical conversations is ~1-12 MB of
text, which is noise next to the DOM and the JS heap a browser tab already
carries.

Blocks are a few times larger in memory than on disk (parsed objects, not JSONL),
and the renderer virtualizes, so the ceiling is heap rather than render cost. Even
so, a pathological 20 MB session would land around a few hundred MB only if
thirty of them were open at once — not a realistic session shape. Not worth a
trim mechanism whose history-window bookkeeping (`historyGeneration` resets,
`oldestItemId` cursors) is itself a source of bugs.

Revisit only if heap profiling on a heavy user says otherwise.

### Layer 3 — `chatStore` (façade)

Keeps app-global state: `activeConversationId`, `redirectToConversationId`,
sticky `selectedEffort` / `selectedModel`, `flashItemId`,
`pendingComposerAttachments`, `queuedMessages` (whose entries already carry a
`conversationId`).

Crucially it **mirrors the active entry's state into its own flat fields**. This
is the compatibility seam. App code touches the store far less than the raw
grep suggests — 88 selector subscriptions and 34 `getState()` calls outside the
store, with `ChatPage.tsx` accounting for 85 of them (the 527/234 figures are the
test file). Mirroring means `useChatStore((s) => s.blocks)` keeps working and
**45 of 46 consumer files need no change at all.**

```ts
switchTo(id):
  unsubscribe mirror from old entry
  activeConversationId = id
  entry = registry.acquire(id)        // attaches only if new
  subscribe mirror: entry.subscribe(s => set(projectionOf(s)))
  set(projectionOf(entry.getState())) // paint immediately
```

No abort, no state wipe, no refetch when already warm. That is the snappy switch.

Actions on the root store become thin delegators to the active entry
(`send: (...a) => registry.active()?.getState().send(...a)`), so ChatPage's
`useChatStore.getState().send(...)` calls are untouched.

**Invariant: per-conversation fields flow entry → root only. The root's copy is
read-only.** Worth stating explicitly because it removes the "which is the source
of truth" ambiguity a bidirectional mirror would create. It holds in app code
today — the only `useChatStore.setState` outside the store and the bench is
`ChatPage.tsx:658`, which writes the app-global `redirectToConversationId`.

Cost of the mirror is one shallow projection per state change on the active
conversation. The pump already coalesces to one `set` per animation frame, so
this is one extra shallow merge per frame; verify with the existing
`loadtest/streamRenderBench.ts`.

The landing route is the one flow that gets restructured rather than moved. `send`
on `/` has no entry yet, so `ensureBoundSession`'s create-then-bind becomes a
root-level `sendNew`: `createSession` → `registry.acquire(id)` → `switchTo(id)` →
`entry.send(...)`. This is cleaner than the current path, which sets
`conversationId` before the navigate callback specifically so ChatPage's URL
effect no-ops.

## 4. What gets deleted

- `evictTranscriptCache`'s call site, which becomes `releaseConversation` (the
  `transcriptCache` LRU itself was already removed by the #4124 revert; this
  work removes the need for any cache at all).
- `pendingByConversation`, `StashedPending`, the `committedTexts` dedupe
  baseline, `removeFromPendingStash`, `committedUserTextsOf`, `contentKeyOf`, and
  the stash/restore block in `switchTo` (~`:1589-1630`). In-flight bubbles simply
  stay on their entry. This kills the "stuck-forever pending bubble" class of bug
  outright.
- The cache-gap-bridge path: `hydrateFromCache`, `backfillItemsUntilCovered`,
  `spliceUnseenAheadOfInFlight`, and the navigate-back half of
  `reconcileOnReconnect`.
- Every ad-hoc `s.conversationId === event.conversationId` guard in
  `handleSessionEvent`.
- Most of `flushBackgroundQueues`. A conversation with a live entry drains itself
  through its own `maybeFlushQueuedHead` on its own status edges. The
  cache-status-driven path stays only for conversations that are not retained.

Keep the *drop-recovery* half of `reconcileOnReconnect` — transport drops and the
~5-minute ingress recycle still happen, and background streams need the same
recovery. Only the navigate-back variants go.

## 5. Presence — deliberately unchanged

Opening a stream registers the user as a viewer, so with 30 live streams a user
shows as an active viewer on every conversation they hold open. **Decision: leave
this as-is, out of scope for this work.**

It is a defensible reading — those conversations genuinely are open and
live-updating in that tab — and the server already absorbs the churn: presence
aggregates per user across connections (`all(connection.idle for ...)`,
`presence.py:242`) and holds a leave-grace window, so ~5-minute stream recycles
do not flicker avatars.

The alternative considered was reporting idle for non-active conversations
(`document.hidden || !isActive`), which the existing `SessionViewer.idle` flag and
its greyed avatar (`PresenceAvatars.tsx:42`) would render for free. Rejected for
now: it needs the entry to know whether it is on screen, which is the only
remaining reason foreground would exist as a concept at all. Revisit if users find
the viewer list misleading.

One piece of presence work is already done, and was needed regardless of the above
(a flip must recycle EVERY open stream, since the `idle` flag rides on each
stream's GET): `presenceAttemptControllers` ✅ `9c44023a`.

## 6. Risks

1. ~~**`handleSessionEvent` conversion is the correctness minefield.**~~ *Done in
   `64c7a087`* — 30 branches, each needing its writes routed and its
   cross-conversation effects left alone. Two traps worth knowing if this code is
   touched again: a mechanical find-and-replace rewrote the guard helper's own
   body into an infinite recursion that `tsc` accepted (only tests caught it),
   and the branches that name their own target need the payload id checked *as
   well as* the delivering stream, not instead of it.
2. **Splitting the 16 `conversationId === id` guards into liveness vs.
   foreground.** Leave them as-is and every background pump exits at its first
   reconnect check — silently degrading to today's behaviour while carrying all
   the new complexity. This is the failure mode that looks like success in
   manual testing, because a freshly-opened conversation works fine and only a
   backgrounded one goes stale after ~5 minutes.

   *Confirmed during phase 1.* An attempt to test two concurrent pumps could not
   even reach its assertion: the second `startStreamPump` exits at
   `get().conversationId !== id` the moment the other conversation becomes
   active (`chatStore.ts:3312`, `:3317`, `:3323`). Until this split lands, no
   test can exercise more than one live stream, which is why the phase-1 presence
   fan-out is pinned as a unit test against the contract rather than end-to-end
   through two pumps. Treat the split as the gate for every multi-stream test.
3. **Reconnect/dedupe interaction.** The invariants in those long comments —
   itemId dedupe across snapshot and buffer, live-preview stamping, elicitation
   revival — are real and load-bearing. They must hold per-entry, and now also
   for entries nobody is watching, across many more reconnects than today.
4. **Test suite migration.** 328 tests, 9376 lines. `getState()` reads keep
   working through the mirror, but the 234 `useChatStore.setState({...})` seeds
   will not reach the entry under the one-directional invariant, and the 122
   direct `handleSessionEvent(ev)` calls change shape. Both are mechanical: add a
   test-only `seedActiveConversation(partial)` helper and rewrite the call sites.

   The tempting alternative is a compatibility `setState` shim that forwards
   per-conversation fields to the active entry, saving most of those edits. I
   recommend against it: it preserves exactly the singleton coupling we are
   paying to remove, and it makes the source of truth ambiguous for the next
   reader. Take the ~350 mechanical edits.
5. **Memory.** Accepted as unbounded per the measurements above (mean 0.30 MB per
   conversation, p90 0.4 MB). Since the #4124 revert main retains no transcripts
   at all, so this is a genuine increase rather than a shift — still single-digit
   MB for 30 typical conversations.
   Revisit only on evidence from heap profiling.

## 7. Phasing

Each phase ships independently. Phases 0-2 are behaviour-preserving by design,
so the existing 328 tests are the regression net; only phase 3 changes semantics.

- **Phase 0** — split the `ChatState` type into `ConversationState` + app-global
  state. Types only, no behaviour change. ✅ `7dfdf0cf`
- **Phase 1** — add `createConversationStore` and move the already-threaded
  stream machinery onto it. Registry capacity 1. Root store mirrors the active
  entry. Deliberately a no-op refactor; the risky commit, validated against the
  current suite.

  Runtime singletons that must become per-entry are being converted first, since
  each is independently shippable and green against the current suite:
  - send-ordering chain, keyed per conversation ✅ `4eb347f9` (also removes a
    cross-conversation head-of-line block)
  - presence attempt controllers, one per live stream ✅ `9c44023a`
  - remaining: `retiredLiveMessages`, `liveLastIndex`, the rAF scheduler and
    `BlockStream` reducer (already per-pump locals — they move with the pump),
    and `racedNativeModelOptions` (already keyed by id; verify only)
- **Phase 2** — route `handleSessionEvent`'s writes by conversation. Still
  capacity 1. ✅ `64c7a087`

  Landed ahead of the entry store, as a parameter (`handleSessionEvent(event,
  streamConversationId)`) plus a central `applyToConversation` gate rather than
  as a method. Same effect — every conversation-scoped write is now routed — but
  shippable and testable against the current suite, and it converts the audit
  into executable tests before the larger refactor moves the code. Becoming a
  method later is mechanical: the gate turns into "write my own store".

  The audit that drove it: of 30 branches, 19 write conversation state and only
  6 checked which conversation the event was for. Also found that events which
  DO name a target need both checks — a session's stream can carry frames about
  another conversation — hence `applyToNamedConversation`.
- **Phase 3** — raise `MAX_LIVE` to its real value (30 prod / 3 dev), convert the
  guards to liveness checks so background pumps keep reconnecting and
  reconciling, and delete `pendingByConversation` and the remaining
  cache-bridge paths. The feature lands here. Presence is untouched (§ 5).
  ✅ `3fc0adb2` (registry) → `8617e6df` (feature) → `f2472cb7` (stagger +
  cache-merge cleanup)

  Net −566 lines across the feature commit. Two bugs surfaced only by wiring it
  up, both now covered by tests: `send` on the landing route pushes its
  optimistic bubble before the session exists (buffered on the root store, then
  adopted by the new entry), and late-settling send work must land on the
  conversation it targeted rather than the visible one.
- **Phase 4** *(optional)* — drop the mirror, migrate components to
  `useActiveConversation(selector)`. Not needed for correctness: the mirror is
  one shallow projection per change on the active conversation, and it is what
  keeps 45 of 46 consumer files untouched.

A follow-on this unlocks: the sidebar could read live per-conversation state
(working spinner, token counts) straight from entries instead of the
`WS /v1/sessions/updates` overlay.

## 8. Open decisions

1. `MAX_LIVE = 30` is set arbitrarily and is cheap to change; the dev value of 3
   is forced by HTTP/1.1. Enabling `server.https` in the dev server (Vite 8 then
   serves HTTP/2) lets dev converge on 30 — worth doing soon after, so developers
   exercise production's eviction rate.
2. ~~Whether the reconnect stagger is worth adding up front~~ — added in
   `f2472cb7`; background entries only.

*Settled:*
- *memory is unbounded by choice (§ Layer 2);*
- *the cap is a single `MAX_LIVE`, no detached tier (§ Layer 2);*
- *reconnect-on-drop is required per entry, and every reconnect reconciles —
  background included, because the gap loses events (§ Layer 2);*
- *presence is left as-is: holding a live stream shows you as an active viewer
  (§ 5);*
- *foreground is not a first-class concept — the guards become liveness, and most
  disappear (§ Layer 2).*
