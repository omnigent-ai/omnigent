# Deploying the Omnigent Slack bot on Databricks Apps

This directory deploys the **Omnigent Slack bot** to
[Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
via [Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

Deploy the bot here when the Omnigent **server** it talks to is itself a
Databricks App (header/proxy auth). In that mode the bot can't drive the usual
device/OIDC login, so it runs a **custom U2M OAuth app** (authorization code +
PKCE, `offline_access`) via an enrollment page it serves as a Databricks App: a
Slack user signs in at the workspace and the bot receives a durable, refreshable
token it forwards to the server. See `[../../docs/DATABRICKS_APP_WEBAUTH_DESIGN.md](../../docs/DATABRICKS_APP_WEBAUTH_DESIGN.md)`
for the full design, and the integration `[README.md](../../README.md)` for how
the bot works otherwise.

Unlike the server app, the bot needs **no Lakebase and no UC volume** — it's a
stateless pure-PyPI package. Mirroring the server deploy, `deploy.py` builds an
`omnigent_slack` wheel, generates an app-level `src/pyproject.toml` + `src/uv.lock`
that point at it, copies the wheel into `src/`, then runs `databricks bundle
deploy` + `bundle run`. The Databricks Apps runtime installs the source
directory with `uv sync`, so the app imports `omnigent_slack` from the built
wheel. Runs unchanged from a laptop; re-runnable.

> The generated `src/*.whl`, `src/pyproject.toml`, and `src/uv.lock` are kept
> **untracked but not git-ignored** — `bundle deploy` respects `.gitignore` for
> its file sync, so git-ignoring them would silently drop them from the upload
> and the app would fail with `ModuleNotFoundError: No module named
> 'omnigent_slack'`.

## Prerequisites

> [!IMPORTANT]
> **Run this app as a single instance — do NOT enable "Horizontal scaling"
> (Beta).** The bot is a Slack Socket-Mode consumer, so a second replica opens a
> second socket and double-processes every event (duplicate turns); and the
> enrollment PKCE verifiers / unconfirmed tokens live in process memory, so a
> callback routed to a different replica fails closed with a confusing "link
> expired". Databricks Apps are single-instance by default — just leave scaling
> off. (There's no Asset Bundle field to pin instance count during the Beta;
> instance count is UI-only, so this is enforced by not turning it on.)

1. A Databricks workspace with Databricks Apps enabled.
2. A **custom U2M OAuth app** ("custom app integration") registered in the
   workspace with the authorization-code grant enabled — see step 1 below for
   its redirect URI and scopes.
3. The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install.md),
   authenticated via a profile (`--profile`) or env auth.
4. A **Slack app** (Socket Mode + Interactivity) with its bot token (`xoxb-…`)
  and app-level token (`xapp-…`) — see the integration README's *Setup*.
5. The **target Omnigent server app** already deployed as a Databricks App (you
  pass its URL as `--server-url`).
6. Permission to create a **secret scope** and grant the app's service principal
  `READ` on it.

Set your workspace URL in `databricks.yml` under `targets.prod.workspace.host`
(it ships as a `https://example.databricks.com` placeholder; DAB reads it before
resolving variables, so it must be a literal).

## One-time setup



### 1. Create the secret scope + keys

The bundle wires four secrets into the app (never plaintext in YAML). Create the
scope and populate the keys:

```bash
databricks secrets create-scope omnigent-slack

databricks secrets put-secret omnigent-slack slack_bot_token          # xoxb-…
databricks secrets put-secret omnigent-slack slack_app_token          # xapp-…

# Fernet key that encrypts stored tokens at rest:
KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
databricks secrets put-secret omnigent-slack token_encryption_key --string-value "$KEY"

# Custom U2M OAuth app secret (see below). The client id is public and passed
# inline via --oauth-client-id, NOT stored here.
databricks secrets put-secret omnigent-slack databricks_oauth_client_secret # <client secret>

# HMAC key signing the enrollment state — its own secret, kept separate from the
# OAuth client secret. Any long random string:
databricks secrets put-secret omnigent-slack databricks_state_secret \
    --string-value "$(openssl rand -hex 32)"
```

> **Register the custom OAuth app first** (a workspace admin, once per
> workspace): create a U2M/"custom app integration" OAuth app in the workspace,
> enable the authorization-code grant, request scopes (`all-apis` by default —
> the token's scopes must be a superset of the target server app's scopes, so
> `all-apis` always works; `openid` + `offline_access` are added automatically),
> and register the redirect URI **`<this-app-url>/auth/callback`**
> (the same app URL you pass as `--webauth-base-url`). Because the app URL only
> exists after the first deploy, register the redirect URI between the first and
> second deploy passes.



### 2. First deploy — creates the app + its service principal

Run [Deploy](#deploy) once. The first `bundle deploy` creates the app and its
service principal (SP).

### 3. Grant the app SP read on the secret scope

```bash
databricks secrets put-acl omnigent-slack <app-service-principal> READ
```

Find the SP with `databricks apps get omnigent-slack -o json | jq -r .service_principal_client_id`
(or the name shown in the Apps UI). Re-deploy after granting.

## Deploy

The enrollment link needs this app's **own public URL**, which the platform does
not inject as an env var and which only exists once the app is created — so the
first deploy is a two-pass step.

**First deploy** (creates the app; enrollment link not yet wired):

```bash
uv run python integrations/slack/deploy/databricks/deploy.py \
    --app-name omnigent-slack \
    --profile <your-profile> \
    --secret-scope omnigent-slack \
    --oauth-client-id <oauth-app-client-id> \
    --server-url https://<server-app>.databricksapps.com
```

Read the app's URL, then **re-deploy** with it:

```bash
APP_URL="$(databricks apps get omnigent-slack -o json | jq -r .url)"

uv run python integrations/slack/deploy/databricks/deploy.py \
    --app-name omnigent-slack \
    --profile <your-profile> \
    --secret-scope omnigent-slack \
    --oauth-client-id <oauth-app-client-id> \
    --server-url https://<server-app>.databricksapps.com \
    --webauth-base-url "${APP_URL}"
```

`deploy.py` builds the wheel, writes `src/pyproject.toml` + `src/uv.lock`, copies
the wheel into `src/`, runs `bundle deploy --target prod`, then
`bundle run omnigent-slack --target prod`. Pass `--skip-run` to deploy without
starting, or `--skip-build` to reuse the existing `src/` wheel + lock. Subsequent
redeploys are a single invocation (keep `--webauth-base-url`).

> On the Databricks network, public PyPI is blocked, so point uv at the internal
> proxy for the lock step — either `--index-url https://pypi-proxy.cloud.databricks.com/simple`
> or `UV_INDEX_URL=…` (the lock is then normalized back to public PyPI for
> reproducibility). See [go/pypi-registry-access](http://go/pypi-registry-access).

## After deploy

1. Confirm the app is **Running** and that the custom OAuth app's redirect URI
   matches this app's `<url>/auth/callback` exactly.
2. In Slack, run `/omnigent`. The modal shows a **Sign in with Databricks**
  link pointing at the workspace `/oidc/v1/authorize`. Complete it; Databricks
   redirects to this app's `/auth/callback`, which shows a **consent page** —
   click **Confirm** to link the accounts, the token is stored, and the modal
   advances to agent/host selection.
3. Grant each intended Slack user **workspace access to the server app** (the
  token only authenticates for users who can reach the server app).



## How it works

- The app binds `DATABRICKS_APP_PORT` (8000) with the OAuth callback web server
(`omnigent_slack/webauth.py`) and, in the same process, runs the Socket-Mode
bot that connects out to Slack.
- **Custom U2M OAuth app (authorization code + PKCE).** The enrollment link is
the workspace `/oidc/v1/authorize` URL; the user signs in and Databricks
redirects back to `/auth/callback` with a single-use, PKCE-bound code. The bot
exchanges it at `/oidc/v1/token` for an **access + refresh** pair
(`offline_access`), shows a consent page naming the identities, and — only on
**Confirm** — stores the token and presents the access token as the bearer to
the server (the server's proxy validates it and injects the real
`X-Forwarded-Email`). The
token is bounded by the OAuth app's scopes.
- **Durable across restarts of the grant, ephemeral on disk.** The SQLite token
store lives on ephemeral disk (`OMNIGENT_DATA_DIR=/tmp/omnigent-slack`),
encrypted at rest; a restart loses it and the user re-enrolls. But within a
grant's life the bot refreshes the access token via the refresh token, so a user
signs in once rather than hourly.



## Configuration reference

Environment wired by `databricks.yml` (secrets via `value_from`, rest inline):


| Variable                                 | Source               | Description                                      |
| ---------------------------------------- | -------------------- | ------------------------------------------------ |
| `OMNIGENT_SLACK_BOT_TOKEN`               | secret               | Slack bot token (`xoxb-…`)                       |
| `OMNIGENT_SLACK_APP_TOKEN`               | secret               | Slack app-level token (`xapp-…`)                 |
| `OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY`    | secret               | Fernet key for tokens at rest                    |
| `OMNIGENT_SLACK_DATABRICKS_CLIENT_ID`    | `--oauth-client-id`  | Custom U2M OAuth app client id (public, inline)  |
| `OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET`| secret               | Custom U2M OAuth app client secret               |
| `OMNIGENT_SLACK_DATABRICKS_STATE_SECRET` | secret               | HMAC key signing the enrollment `state`          |
| `OMNIGENT_SLACK_DATABRICKS_SCOPES`       | inline (optional)    | Requested scopes (default `all-apis`; must be a superset of the server app's scopes; `openid` + `offline_access` forced on) |
| `OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST` | inline (optional)  | OAuth app's workspace host (defaults to `DATABRICKS_HOST`) |
| `OMNIGENT_SLACK_SERVER_AUTH`             | inline               | `databricks` (selects the OAuth mode)            |
| `OMNIGENT_SERVER_URL`                    | `--server-url`       | Omnigent server the bot drives                   |
| `OMNIGENT_SLACK_WEBAUTH_BASE_URL`        | `--webauth-base-url` | This app's public URL — link base + redirect URI |
| `OMNIGENT_DATA_DIR`                      | inline               | Ephemeral SQLite store dir                       |
| `DATABRICKS_APP_PORT`                    | Databricks runtime   | Port the callback server binds (8000)            |




## Troubleshooting


| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'omnigent_slack'` | The wheel/`pyproject.toml`/`uv.lock` were git-ignored, so `bundle deploy` didn't sync them | Ensure `src/*.whl`, `src/pyproject.toml`, `src/uv.lock` are untracked but NOT git-ignored; re-run `deploy.py` (not `--skip-build` on a clean `src/`) |
| `uv lock` fails with a PyPI DNS error | Public PyPI blocked on the Databricks network | Re-run with `UV_INDEX_URL=https://pypi-proxy.cloud.databricks.com/simple` |
| App install fails; `/logz` shows an `exclude-newer` re-resolve then a PyPI timeout | Runtime's uv `exclude-newer` cutoff differs from the lock's | Read the cutoff from `/logz` and pass it via `--exclude-newer <cutoff>`, then redeploy |
| Sign-in ends on an OAuth error page (redirect mismatch) | The OAuth app's redirect URI ≠ `<this-app-url>/auth/callback` | Register the exact `/auth/callback` URL on the custom OAuth app |
| Sign-in page says the link was already used or expired | The redirect was replayed, or the bot restarted between link-issue and callback (in-memory PKCE verifier lost) | Run `/omnigent` again for a fresh link |
| Enrolled, but turns fail auth against the server                      | User lacks access to the server app, or the token's scopes don't satisfy the server proxy | Grant the user server-app access; widen `OMNIGENT_SLACK_DATABRICKS_SCOPES` if the server proxy needs more |
| App boots but Slack shows no sign-in link                             | `--webauth-base-url` not passed (the app URL only exists after first deploy) | Re-deploy with `--webauth-base-url "$(databricks apps get <app> -o json | jq -r .url)"` |
| App can't read secrets                                                | App SP missing scope ACL                                                     | `databricks secrets put-acl <scope> <sp> READ`, redeploy                                |
| Plan shows destroy/replace of the app                                 | `--app-name` mismatch vs. tracked state                                      | Re-check `--app-name`; state is per-app under `root_path`                               |




## Files in this directory


| File | Purpose |
| --- | --- |
| `databricks.yml` | DAB bundle config — app resource, secrets, env. |
| `deploy.py` | Orchestrator: build wheel → write `pyproject.toml`/`uv.lock` → deploy + run. |
| `src/app.py` | App entry point — runs `omnigent_slack.app.run()`. |
| `src/app.yaml` | App startup config (command + env). |
| `src/*.whl`, `src/pyproject.toml`, `src/uv.lock` | Generated per deploy by `deploy.py`; untracked, not git-ignored. |


