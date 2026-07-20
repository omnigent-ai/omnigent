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
only a `status` field. If no runner is currently registered, the server
first tries to wake the session's existing host/workspace binding and
initializes the relaunched runner before retrying the goal control. The
caller cannot choose a new host or workspace through these routes.

All routes are valid only for sessions stamped with
`omnigent.wrapper=codex-native-ui`.

```
GET /v1/sessions/{session_id}/codex_goal
```

Auth: read access to the session. If the session's runner is offline
and the server must relaunch the stored host binding, edit access is
required for that wake-up side effect.

200 OK:

```
{
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

`goal` is `null` when Codex reports no persisted goal. `created_at`
and `updated_at` may be `null` if the app-server response omits them.
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
