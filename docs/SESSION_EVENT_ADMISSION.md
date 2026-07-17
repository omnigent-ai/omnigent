# Experimental session-event admission

Omnigent exposes an opt-in Python extension seam for integrations that need an
atomic answer to this question before a user message is persisted: does the
message start a turn, steer the active turn, or wait for the next turn?

The seam is experimental. Applications that do not configure it retain the
stock event path.

## Configure an admitter

Pass a `SessionEventAdmitter` to the public `create_app(...)` factory. The
admitter selects sessions; returning `False` makes no reservation call and
leaves the acknowledgement unchanged.

```python
from omnigent.admission import AdmissionInfo, SessionInfo
from omnigent.server.app import create_app


class RoutedSessions:
    async def wants(self, session: SessionInfo) -> bool:
        return session.labels.get("routing.mode") == "external"


async def record_admission(
    session_id: str,
    item_id: str | None,
    admission: AdmissionInfo,
) -> None:
    print(session_id, item_id, admission.lineage_id)


app = create_app(
    agent_store,
    file_store,
    conversation_store,
    artifact_store,
    agent_cache,
    session_event_admitter=RoutedSessions(),
    on_event_admitted=record_admission,
)
```

For a selected user message, Omnigent reserves the runner's FIFO admission
slot before REQUEST policy evaluation. `EvaluationContext.admission` then
contains:

- `admission_id`: one-shot reservation identifier;
- `input_seq`: monotonic input sequence for the session;
- `disposition`: `new_turn`, `active_steer`, or `next_turn_buffer`;
- `lineage_id`: stable identifier for the model-call lineage;
- `active_response_id`: active response identifier, when one exists.

An allowed message consumes the reservation exactly once. A denied, declined,
timed-out, or disconnected request cancels it. Unconsumed reservations expire
after the runner's configured TTL (30 seconds by default). Reuse, expiry,
foreign-session use, and post-restart use return named errors rather than
silently admitting the message again.

The handled event acknowledgement gains an additive `admission` object, and
the optional `on_event_admitted` callback receives the same correlation after
persistence. If reservation fails for a selected session, the request fails
closed with `admission_unavailable`; it never falls through to an uncorrelated
event path.

## Compatibility contract

- `session_event_admitter=None` is the default and makes no new runner calls.
- Unselected sessions receive no new fields and no admission callback.
- `EvaluationContext.admission` is optional and defaults to `None`.
- Admission state is transient runner state; no database migration is needed.
- The extension observes and correlates Omnigent's turn decision. It does not
  change model routing, buffering, steering, or turn ordering.

