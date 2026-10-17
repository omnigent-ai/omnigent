# Session observation ownership

A runner-offline sweep may outlive its runner binding. Runner A can disappear,
the session can bind to B, and the sweep can finish before B has a relay on the
old replica. Checking only local relay identity or conditionally updating
`live_status` does not protect the later cache, stream, label, child, or
scheduled-run effects.

The metadata row owns a binding epoch and live-state revision. The epoch
advances only when the runner ID changes, including clearing it. A→B→A therefore
has a different epoch from the original A binding. Status, response identity,
and error identity define one observation. A meaningful change advances the
integer revision; repeated busy signals retain the revision and active response.
New busy activity after a terminal state clears an omitted response ID, so a
later no-ID turn cannot reopen the old turn or reuse its source-failure identity.
An observation ID identifies an exact queued observation for promotion and
idempotence. Its value has no ordering meaning.

Runner publishers capture the authenticated runner binding before transport or
enrichment awaits. Their queued writes require the complete captured observation,
including its payload at the expected epoch and revision. User actions that
express current intent may resolve the current binding on the ordered worker.
Store transactions perform metadata changes only. The worker publishes the
canonical version after commit, outside transaction retries. It promotes only the
exact predecessor captured before an await; a later same-runner turn or error
cannot become an old callback's new identity. Response-start events record turn
identity even when the runner's busy signal omitted a response ID.
Missing response IDs inherit only from that captured lifecycle, never from a
newer global pending edge.

An offline decision records the complete captured observation, episode ID,
error-label projection, old child turn, and old scheduled-run ID in one metadata
transaction. It applies only while that exact observation is current. Rebinding
or a newer normal observation invalidates it. Sweep retries retain the original
candidate rather than adopting a newer turn from the cache. Repeated busy observations do not
recover an owned failure; an explicit recovery or changed status, response, or
error does. Duplicate decisions retain the first cause.

Normal status errors use the same metadata transaction as their revision.
Error enrichment is staged with its captured owner rather than written late to
the conversation database. Hydration overlays the current error payload onto
the managed error-label keys, preserving custom labels and the historical raw
labels in the conversation database. A newer healthy observation clears the
current projection. An uninformative failed probe keeps the existing cause.

Cache, stream, and client reducers compare binding epoch, revision, then owned
failure priority. Complete observation identity must match at equal epoch and
revision. A normal observation cannot undo its derived failure at the same
revision. A new revision or epoch can. Old snapshots follow the same rule as
old stream events. Child summaries carry this fence; child activity names the
captured old turn. Inferred offline child markers remain historical rows, but
store reads omit their projection once the decision is obsolete. Confirmed
normal source outcomes remain visible history and retain their initiating-request
idempotence; their annotations fence only late live effects. Pagination
still advances by underlying cursors, including empty visible pages; unavailable
child metadata suppresses only these markers and preserves ordinary history.
Cache eviction retains the learned fence against late legacy setters.

A committed interruption settles its exact captured scheduled-run ID even if a
later runner supersedes its display. The scheduled store conditionally updates
only a still-running firing, so a completed old run and a distinct new run keep
their outcomes. A new firing uses a new ID; terminal runs cannot be reopened by
the store API.

A committed genuine interruption logs once with the captured owner, epoch,
revision, and episode, even when a later binding makes its display effects
obsolete. A rejected stale decision emits no new turn failure. This preserves
real failure accounting. Explicit source failures are different from inferred
runner loss: their captured source identity is logged once per process even
when metadata persistence is unavailable. A stable source-event ID, captured
runner, and `observation_committed=false` identify this evidence. Callback and
persistence retries reuse that identity and do not add duplicate failure rows.
Their cache, labels, and stream projection still require a committed version.
Native status routes await that result before recording or forwarding terminal
effects. A captured durable sub-agent dispatch ID travels with the observation;
the runner checks its immutable work ID before status/fanout, after recovery
awaits, and at each terminal reducer. Recovery binding changes atomically require
the accepted observation, including for an A→B→A binding sequence.

Native ensure and host launch failures retain their owner before transport
awaits. Their source input/error history remains durable even when a newer
turn rejects the status. Accepted failures carry the canonical observation
and captured dispatch to the parent; live error and startup signals also carry
the full observation so another replica's newer turn can reject them. A
server-owned setup failure may introduce its newly persisted response only at
the exact captured binding and revision. Ordinary runner terminal callbacks
retain the existing different-response rejection. Client stable IDs keep
consumed setup inputs and their response identity stable across retries.

Native initialization, terminal ensure, and host launch reserve a new canonical
revision before asking the provider or accepting input. The last normal status,
response, and error payload stay intact; the new startup intent invalidates a derived offline overlay
without changing its original attribution. The immutable reservation token
makes a lost write acknowledgement idempotent. The same transaction persists
the pending boolean, so another replica reads the actual startup state. An
exact completion clears it with another revision and a deterministic completion
token; provider work and accepted input are never retried by metadata retries.
The reservation also owns a separate durable claim and an exact normal-state
lineage. A prior turn's idle completion may advance that lineage under its full
captured-owner CAS while retaining the immutable reservation. Startup completion
checks both the original reservation and its protected lineage, preserving the
completed turn's normal result. Busy or new-turn activity, failure, an incompatible
or unowned idle write, binding changes, and offline settlement invalidate the
claim. A completed claim keeps only its exact retry receipt until later work.
Startup signals retain this canonical owner independently of the status
cache, so a newer startup before any running edge still rejects an old
completion or snapshot. Snapshot fields come from one captured row and its
token, even when a newer cache floor is retained.
An explicitly annotated runner status keeps its exact producer owner. A malformed
owner cannot mutate shared state; later ordinary source history still persists.
If a new startup fails with the same prior error payload, its exact reservation
still completes with a new failure token and clears the pending state.

The runtime stream learns accepted status and hydration observations before
recording annotated events into replay buffers or fanning them out. An old
annotated terminal event cannot clear a newer response's replay. Relay flushes
also require their captured observation and response identity when resetting
the shared in-flight text; committed source history remains available.

The ingress-captured dispatch ID proves that the dispatch did not change during
the callback. It cannot identify an already-delayed legacy callback that first
arrives after a newer dispatch started. An explicit old response ID is rejected
against a different busy response; no-ID callbacks remain ambiguous until the
producer carries immutable turn identity. Their genuine source failures must
remain accounted for rather than being hidden or relabeled as a newer turn.

## Compatibility and unavailable metadata

The migration initializes epochs and revisions to zero. A default-zero row
without an observation ID remains legacy until it gains a durable observation
or binding change. Optional wire fields preserve older parsers. Once a consumer
learns a version, an unversioned lifecycle edge cannot rewind it. Metadata-only
partial updates may still update unrelated fields.
Unowned legacy recovery causes remain visible across binding changes until a new
execution observation settles them. Canonical causes and causes naming an old
runner are cleared by the binding change.

Full protection requires all application-server writers and consumers to use
the protocol. Older servers can change raw state without advancing a revision,
and older clients cannot reject a delayed versioned edge. During a mixed-version
deployment, a legacy writer's raw error labels may be superseded by a newer
server's canonical projection. Roll out the migration before upgraded writers
and complete the server/client rollout before claiming projection closure.

Metadata outages cannot establish ownership. Configured servers keep the last
accepted display state and log failed writes rather than publishing an
unguarded failure. Normal writes retry the same captured owner, predecessor,
token, and payload three times on the ordered worker, with 0.1/0.25-second
backoff. A transient
outage can recover a one-shot failure without a producer republish. Exhaustion
returns a retryable error to a waiting native callback and preserves its source
failure log; it cannot guarantee display recovery after an arbitrary outage.
Offline sweeps retry transient errors and report exhausted attempts. A later
sweep can settle the still-active observation after storage recovers. No error
rate or successful settlement should be inferred from a rejected or unavailable
projection. Unconfigured in-process harnesses retain legacy publication.

## Verification

Run the observation store, projection, relay, and migration suites:

```sh
pytest tests/stores/test_session_observation.py \
  tests/server/test_session_observation_projection.py \
  tests/server/routes/test_sessions_runner_relay.py \
  tests/db/test_migration_session_observation.py
```

For a manual multi-replica check, start a turn on A, disconnect its relay, and
rebind the session to B while the old sweep is paused. Start B's next turn and
release the sweep. The open conversation, sidebar, parent Agents rail, and a
fresh snapshot should keep B's turn and cause; B's scheduled run must remain
running. Repeat without a rebind: the interrupted A turn should fail once and
show its disconnect cause after refresh.
