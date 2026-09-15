# Connect Databricks (per-user) + MCP through the AI Gateway

## Motivation

Today a user connects **GitHub** (per-user OAuth) and the sandbox gets a GitHub
MCP wired directly to GitHub's hosted MCP. We want the same "Connect …" flow for
**Databricks**, so that:

1. The user authenticates to their Databricks workspace once (like GitHub), and
2. The sandbox's MCP tools are routed through the **Databricks AI Gateway (AIGW)**
   — GitHub and other tools are configured there as MCP Services — instead of
   each tool being wired up directly. So the direct GitHub MCP is **replaced** by
   the AIGW MCP when Databricks is connected. (GitHub *clone* still uses the
   GitHub credential broker — MCP can't `git clone`.)

This is the "AIGW-primary MCP" track anticipated in
[CREDENTIAL_STORE.md](CREDENTIAL_STORE.md); this doc pins the decisions and the
concrete shape.

## Decisions (settled)

- **Auth = OAuth U2M authorization-code + PKCE**, `offline_access` for
  server-side refresh — a mirror of the GitHub App connect flow.
- **MCP = replace the direct GitHub MCP** with an AIGW-routed MCP when Databricks
  is connected.

## Reuse: no new storage

The credential store is already provider-agnostic (`integration_connections`
keyed by `(workspace_id, user_id, provider, account_id)`), so Databricks is a new
`provider="databricks"` façade over the **existing** `IntegrationCredentialStore`
— **no new table, no migration**, and it inherits the KMS-backed
`SecretCipher` at rest.

- secret (encrypted): `{access_token, refresh_token}`
- metadata (plain): `{workspace_host, databricks_user, token_expires_at,
  refresh_token_expires_at, scopes}`

## Databricks OAuth U2M

Databricks is multi-workspace, so the user supplies their **workspace host** at
connect time (like `omni login <workspace-url>`), and the OAuth endpoints are
workspace-relative:

- authorize: `https://<workspace-host>/oidc/v1/authorize`
- token: `https://<workspace-host>/oidc/v1/token`

Flow (parallels `integrations_github.py`, with two Databricks-specific twists):

1. `GET /v1/integrations/databricks/connect?workspace=<host>&return_to=…` →
   generate a PKCE `code_verifier`/`code_challenge`, sign a state JWT carrying
   `{sub, workspace_host, code_verifier, return_to, nonce, exp}` (HS256, same
   `_sign_state` pattern), redirect to the workspace authorize URL with
   `response_type=code`, `client_id`, `redirect_uri`, `scope`,
   `code_challenge`, `code_challenge_method=S256`, `state`.
2. `GET /v1/integrations/databricks/callback` → verify state (rebind `sub` to the
   authenticated caller), POST to the workspace token URL with the `code` +
   `code_verifier` (+ `client_secret` if confidential), store the token set via
   `DatabricksConnectionStore.upsert`, redirect with `?databricks=connected`.
3. `GET /v1/integrations/databricks/status`, `POST …/disconnect` — as GitHub.

**Twists vs GitHub:** (a) per-workspace host means the host is data, carried in
state and stored in metadata; (b) PKCE means the `code_verifier` must survive the
round trip — carried inside the signed state (it never leaves our server in a
usable form, and the state is short-TTL + signed).

Config (`DatabricksConfig.from_env`, mirroring `GitHubAppConfig`):
`OMNIGENT_DATABRICKS_CLIENT_ID` (required — the account custom-app-integration
id), `OMNIGENT_DATABRICKS_CLIENT_SECRET` (optional; omit for a public/PKCE
client), `OMNIGENT_DATABRICKS_SCOPES` (default `all-apis offline_access`),
redirect derived from `OMNIGENT_DOMAIN`
(`/v1/integrations/databricks/callback`). Unset client id ⇒ feature dormant.

Refresh: `databricks_identity.resolve_databricks_access_token(user_id)` mirrors
`github_identity.resolve_access_token` — refresh when within the expiry margin via
the workspace token endpoint using the stored refresh token, persist with
`update_secret`, return a valid token or `None`.

## Credential broker

`GET /v1/hosts/{host_id}/databricks-credential` (new `routes/host_databricks.py`,
mirrors `host_github.py`): resolve `(host_id, launch_token)` → owner →
`resolve_databricks_access_token`, return `{connected, token, workspace_host}`
with `Cache-Control: no-store`. The raw token never lands on sandbox disk; the
proxy fetches it per-connection via the broker (the same
broker-coords-in-env-not-token pattern the GitHub MCP uses).

## MCP through AIGW (replaces the GitHub MCP)

AIGW MCP is Streamable HTTP with `Authorization: Bearer <databricks-token>` — the
same transport/auth shape the GitHub hosted MCP uses, so this mirrors
`github_mcp_proxy.py`:

- `databricks_mcp_proxy.py` — a local stdio MCP server that forwards to the AIGW
  MCP URL(s), with `_BrokerAuth` fetching the Databricks token from the databricks
  broker and refreshing on 401. AIGW URL forms:
  - managed: `https://<workspace-host>/api/2.0/mcp/{functions|genie|ai-search}/…`
  - MCP Service (third-party tools, e.g. GitHub):
    `https://<workspace-host>/ai-gateway/mcp-services/<service-name>`
- `databricks_mcp.py` — `databricks_mcp_server_config(...)` producing the injected
  `MCPServerConfig` (`name="databricks"`, stdio, `-m
  omnigent.databricks_mcp_proxy`, broker-coords env). Which AIGW MCP endpoints to
  expose is config-driven (`OMNIGENT_DATABRICKS_MCP_SERVICES`).

**Injection (replace):** at the two existing sites —
`omnigent/runner/native/orchestration.py` (~line 995, opencode) and
`omnigent/claude_native_bridge.py` (~line 1120, claude) — when the launching
user has a Databricks connection, inject the `databricks` server **instead of**
the `github` server. Selection is by connection state (Databricks connected ⇒
AIGW), so it's a runtime choice, not a redeploy. GitHub clone is untouched (still
the git credential helper via the GitHub broker).

## Server + frontend wiring

- `create_app`: `databricks_config`/`databricks_store`/`databricks_client` in
  `app.state`, a `databricks_enabled` gate, mount `create_host_databricks_router`
  + `create_integrations_databricks_router`, and a **new** ServerInfo field
  `databricks_app_enabled` (distinct from the existing `databricks_features`
  deploy-mode hint). `cli.py` constructs `DatabricksConfig.from_env()` +
  `DatabricksConnectionStore(db_uri, build_secret_cipher())` next to the GitHub
  block (same KMS cipher instance).
- Frontend: `web/src/lib/databricksIntegration.ts` + a `DatabricksIntegration
  Control` added to the existing `IntegrationsSection` in `SettingsPage.tsx`
  (workspace-URL input → connect); `databricks_app_enabled` in `capabilities.ts`
  + nav gating.

## Provisioning prerequisites (blockers for E2E)

1. **A Databricks OAuth custom app integration** must be registered (admin) to
   get a `client_id` (+ secret if confidential) and to register the redirect URI
   `https://<omni-domain>/v1/integrations/databricks/callback`:
   ```
   databricks account custom-app-integration create --json '{
     "name": "omnigent-github-mvp",
     "redirect_urls": ["https://omnigent-github-mvp.sandbox.caffeine.ai/v1/integrations/databricks/callback"],
     "scopes": ["all-apis", "offline_access"],
     "confidential": true
   }'
   ```
2. **An AIGW MCP Service** (e.g. GitHub) configured in the target workspace, so
   there is something to route MCP through.

Neither can be done from omnigent — both are Databricks account/workspace admin
actions, exactly as the GitHub App had to be registered before the GitHub
connect flow could be verified.

## Open questions

- **Account- vs workspace-level OAuth app:** workspace-level authorize is simplest
  and matches `omni login <workspace-url>`; an account-level app would let one
  registration span workspaces (authorize at
  `https://accounts.cloud.databricks.com/oidc/accounts/<account-id>/v1/authorize`).
- **Which AIGW MCP endpoints to expose by default** — just the GitHub MCP Service
  (to replace today's GitHub MCP), or also managed UC/Genie MCP.
- **Scopes** — `all-apis` is broad; narrow to the specific scopes AIGW MCP needs
  once confirmed.
