# Codex Server API

Codex-specific routes and behavior layered on top of the main
[Omnigent Server API](API.md).

## Codex Goal

Codex-native sessions expose Codex app-server's persisted thread goal as
a live subresource. Goal state is not mirrored into Omnigent labels or
conversation rows; the server validates session access and forwards a
generic goal control event to the bound runner, which calls Codex
app-server (`thread/goal/get`, `thread/goal/set`, or
`thread/goal/clear`). Pause/resume use Codex `thread/goal/set` with
only a `status` field. Reads never launch a runner or native backend. For
mutations, if no runner is registered, the server first tries to wake the
session's existing host/workspace binding and initializes the relaunched
runner before retrying the goal control. The caller cannot choose a new
host or workspace through these routes.

After session access checks, forwarding has a total deadline of 50 seconds
for reads or 180 seconds for mutations, including runner recovery and
initialization. An expired deadline returns 504 with error code
`codex_goal_request_timeout`. A timed-out mutation may have taken effect;
refresh the goal before retrying. Cancellation does not guarantee that the
native backend stopped the mutation.

All routes are valid only for sessions stamped with
`omnigent.wrapper=codex-native-ui`.

```
GET /v1/sessions/{session_id}/codex_goal
```

Auth: read access to the session. Goal reads have no wake-up side effect.

200 OK:

```
{
  "available": true,
  "goal": {
    "thread_id": "thr_123",
    "objective": "Finish the migration and keep tests green",
    "status": "active",
    "token_budget": 40000,
    "tokens_used": 1200,
    "time_used_seconds": 180,
    "created_at": 1776272400,
    "updated_at": 1776272460
  }
}
```

`available: true` means the native backend confirmed the response. In that
case, `goal: null` confirms that Codex has no persisted goal. A non-strict
read that cannot reach a runner, or encounters a verified idle native
bridge, returns `{"available": false, "goal": null}`. Retain the last
accepted goal and any draft or read error for this unavailable response.
It does not confirm deletion.

Send `X-Omnigent-Codex-Goal-Read: strict` for an explicit read that must
confirm native state. Unavailable strict reads return 503; malformed runner
responses return 502, and structured runner errors retain their status.
Strict reads also leave an idle backend stopped.

Older server or runner responses may omit `available`; its absence makes
no new availability claim. In particular, an unmarked background
`goal: null` cannot distinguish an unavailable backend from deletion.
Explicit strict reads keep their existing response/error contract.
The availability field belongs only to GET; write responses retain their
existing schemas.

`created_at` and `updated_at` may be `null` if the app-server response omits them.
`status` is forwarded exactly as Codex reports it; Omnigent does not
rename or normalize lifecycle states.

```
PUT /v1/sessions/{session_id}/codex_goal
Content-Type: application/json

{
  "objective": "Finish the migration and keep tests green",
  "token_budget": 40000,
  "status": "active"
}
```

Auth: edit access to the session.

Request body:

  objective (string, required)
    Goal objective. Trimmed by the server; must be non-empty and at
    most 4000 characters, matching Codex app-server.

  token_budget (integer or null, optional)
    Positive token budget forwarded to Codex as `tokenBudget`. Explicit
    `null` clears the Codex token budget. Omitting the field leaves the
    budget field absent from the forwarded app-server request.

  status (string or null, optional)
    `active` starts or resumes the goal. `paused` stores the goal paused.
    Omitting the field preserves Codex's current lifecycle state.

200 OK - same `{"goal": ...}` shape as `GET`.

```
PATCH /v1/sessions/{session_id}/codex_goal/status
Content-Type: application/json

{
  "status": "paused"
}
```

Auth: edit access to the session.

Request body:

  status (string, required)
    `paused` pauses the active goal. `active` resumes a paused,
    blocked, or usage-limited goal. Codex may report other lifecycle
    statuses (`blocked`, `usageLimited`, `budgetLimited`, `complete`),
    but those are Codex-owned states rather than separate user actions.

200 OK - same `{"goal": ...}` shape as `GET`.

```
DELETE /v1/sessions/{session_id}/codex_goal
```

Auth: edit access to the session.

200 OK:

```
{"cleared": true}
```

Failure modes:

  400 Bad Request
    Session is not Codex-native, objective is blank, or token_budget is
    not positive / null.

  404 Not Found
    No session with that id.

  403 Forbidden
    Caller can read the session but cannot relaunch an offline
    host-bound runner.

  503 Service Unavailable
    No live runner can reach the loaded Codex bridge, the session has no
    usable bound host to relaunch, the runner rejects the goal control,
    or the runner returns a malformed goal payload.

  504 Gateway Timeout
    The total forwarding deadline expired. Mutation outcomes are uncertain;
    refresh the goal before retrying.
