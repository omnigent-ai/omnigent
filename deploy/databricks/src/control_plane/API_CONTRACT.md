# GTM Control-Plane — API Contract

Front-layer control plane for the Databricks Apps deployment. All routes
mounted under `/v1/control-plane`. Lives in `deploy/databricks/src/control_plane/`,
wraps upstream `create_app()` — **no edits to `omnigent/` core**.

Identity: `X-Forwarded-Email` header (Databricks Apps proxy). Groups: resolved
from Databricks SCIM via `WorkspaceClient` (app SP context), cached; overridable
by env for tests/non-Databricks.

## Roles

Three tiers, resolved from group membership (env-configured group→role map):
- `admin` — platform team. Full control. (Native `is_admin` is a superset → admin.)
- `contributor` — may publish agents + manage visibility of agents they own.
- `consumer` — use-only. Cannot publish. Default for everyone else.

### `GET /v1/control-plane/me`
Returns the caller's resolved role and capabilities.
```json
{
  "user_id": "alice@databricks.com",
  "role": "contributor",
  "groups": ["gtm-contributors", "field-eng"],
  "is_platform_admin": false,
  "capabilities": { "can_publish": true, "can_manage_visibility": true, "can_view_usage": true, "can_manage_all": false }
}
```
Unauthenticated → `401`.

## Per-agent visibility

Each template (built-in) agent carries a visibility record:
- `visibility`: `"org"` (all org users) or `"restricted"` (named users/groups).
- `audience`: `{ "users": [...emails], "groups": [...group names] }` (only meaningful when restricted).
- `owner_id`: email of publisher/owner (platform-set).
Agents with no record default to `org` (back-compat for operator-seeded agents).

### `GET /v1/control-plane/agents`
Admin/contributor management list — every template agent with its visibility metadata.
```json
{ "data": [
  { "id": "ag_abc", "name": "deal-helper", "description": "…",
    "visibility": "restricted", "audience": { "users": ["bob@x.com"], "groups": ["fsi-team"] },
    "owner_id": "alice@databricks.com", "created_at": 1782000000, "viewer_can_manage": true }
] }
```
Consumer → `403`.

### `PATCH /v1/control-plane/agents/{agent_id}/visibility`
Admin (any agent) or owner (own agent). Sets visibility + audience.
```json
// request
{ "visibility": "restricted", "audience": { "users": ["bob@x.com"], "groups": ["fsi-team"] } }
// response: the updated record (same shape as a GET item)
```
Not admin/owner → `403`. Unknown agent → `404`.

## Delegated registration (publish)

### `GET /v1/control-plane/publishable`
Contributor+ — the caller's session-scoped agents eligible to publish (sessions
they own that carry an agent).
```json
{ "data": [ { "session_id": "conv_x", "agent_id": "ag_s1", "name": "my-agent", "title": "…" } ] }
```

### `POST /v1/control-plane/agents/publish`
Contributor+ only. Promotes a session-scoped agent into the shared template
catalog by reusing its content-addressed bundle (no re-upload). Platform sets
owner (caller) + visibility. Writes an audit row.
```json
// request
{ "source_session_id": "conv_x", "name": "deal-helper", "description": "…",
  "visibility": "restricted", "audience": { "users": [], "groups": ["fsi-team"] } }
// response
{ "agent_id": "ag_new", "name": "deal-helper", "owner_id": "alice@databricks.com", "visibility": "restricted" }
```
Consumer → `403`. Duplicate template name → `409`. Source not owned by caller → `403/404`.

## Enforcement (cross-cutting middleware — no dedicated UI)

Single `@app.middleware("http")` added in front of upstream routes:
- `GET /v1/agents` — response filtered to agents the caller may see
  (org-visible, or in audience, or owner, or admin).
- `POST /v1/sessions` — when binding a restricted **template** agent the caller
  is not in the audience for, request is rejected with `403` before the session
  is created. (Session-scoped agents stay gated by upstream's READ check.)

## Org-wide usage

### `GET /v1/control-plane/usage`
Admin/contributor — per-agent usage + cost, aggregated from
`conversations.session_usage` (existing cost path) attributed to the session
owner.
```json
{ "data": [
  { "agent_id": "ag_abc", "agent_name": "deal-helper",
    "total_cost_usd": 12.34, "total_tokens": 456789, "session_count": 7,
    "by_user": [ { "user_id": "bob@x.com", "cost_usd": 8.1, "session_count": 4 } ] }
], "totals": { "total_cost_usd": 30.0, "total_tokens": 900000, "session_count": 20 } }
```
`?agent_id=ag_abc` → single-agent drill-down. Consumer → `403`.

## Audit

### `GET /v1/control-plane/audit`
Admin only. Recent governed actions (publish, visibility change).
```json
{ "data": [ { "id": 1, "ts": 1782000000, "actor": "alice@databricks.com",
  "action": "publish", "agent_id": "ag_new", "detail": "visibility=restricted" } ] }
```
