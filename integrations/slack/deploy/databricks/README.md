# Deploying the Omnigent Slack bot on Databricks Apps

This directory deploys the **Omnigent Slack bot** to
[Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
via [Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

Deploy the bot here when the Omnigent **server** it talks to is itself a
Databricks App (header/proxy auth). In that mode the bot can't drive the usual
device/OIDC login, so it runs its own **web-auth enrollment page** as a
Databricks App with user authorization: a Slack user signs in through this
app's proxy, and the bot forwards their proxy-issued token to the server
(Databricks on-behalf-of). See `[../../docs/DATABRICKS_APP_WEBAUTH_DESIGN.md](../../docs/DATABRICKS_APP_WEBAUTH_DESIGN.md)`
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

1. A Databricks workspace with Databricks Apps enabled, and **user
  authorization** available (Public Preview; a workspace admin enables it).
2. The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install.md)
  **>= 0.246.0** (older versions don't support `user_api_scopes`), authenticated
   via a profile (`--profile`) or env auth.
3. A **Slack app** (Socket Mode + Interactivity) with its bot token (`xoxb-…`)
  and app-level token (`xapp-…`) — see the integration README's *Setup*.
4. The **target Omnigent server app** already deployed as a Databricks App (you
  pass its URL as `--server-url`).
5. Permission to create a **secret scope** and grant the app's service principal
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

# HMAC key signing the enrollment state — any long random string:
databricks secrets put-secret omnigent-slack databricks_state_secret \
    --string-value "$(openssl rand -hex 32)"
```



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
    --server-url https://<server-app>.databricksapps.com
```

Read the app's URL, then **re-deploy** with it:

```bash
APP_URL="$(databricks apps get omnigent-slack -o json | jq -r .url)"

uv run python integrations/slack/deploy/databricks/deploy.py \
    --app-name omnigent-slack \
    --profile <your-profile> \
    --secret-scope omnigent-slack \
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

1. In the Apps UI, confirm the app is **Running** and shows the user
  authorization scope (`iam.current-user:read`). If the UI prompts for scope
   consent, approve it.
2. In Slack, run `/omnigent`. The modal shows a **Sign in with Databricks**
  link pointing at this app's `/auth/callback`. Complete it; the modal advances
   to agent/host selection.
3. Grant each intended Slack user **workspace access to the server app** (the
  forwarded token only authenticates for users who can reach the server app).



## How it works

- The app binds `DATABRICKS_APP_PORT` (8000) with the enrollment web server
(`omnigent_slack/webauth.py`) and, in the same process, runs the Socket-Mode
bot that connects out to Slack.
- **User authorization** (`user_api_scopes: [iam.current-user:read]`) makes the
platform inject `x-forwarded-access-token` on the enrollment request. The bot
stores that token and presents it as the bearer to the server (Databricks
on-behalf-of — the server's proxy validates it and injects the real
`X-Forwarded-Email`). The token is scoped to `user_api_scopes`, so it can't act
as a broad workspace credential.
- **No durable storage.** The SQLite token store lives on ephemeral disk
(`OMNIGENT_DATA_DIR=/tmp/omnigent-slack`); tokens are encrypted at rest and
simply re-enrolled after a restart (no refresh token, same model as the
server's `oidc` mode).



## Configuration reference

Environment wired by `databricks.yml` (secrets via `value_from`, rest inline):


| Variable                                 | Source               | Description                                      |
| ---------------------------------------- | -------------------- | ------------------------------------------------ |
| `OMNIGENT_SLACK_BOT_TOKEN`               | secret               | Slack bot token (`xoxb-…`)                       |
| `OMNIGENT_SLACK_APP_TOKEN`               | secret               | Slack app-level token (`xapp-…`)                 |
| `OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY`    | secret               | Fernet key for tokens at rest                    |
| `OMNIGENT_SLACK_DATABRICKS_STATE_SECRET` | secret               | HMAC key signing enrollment `state`              |
| `OMNIGENT_SLACK_SERVER_AUTH`             | inline               | `databricks` (selects web-auth mode)             |
| `OMNIGENT_SERVER_URL`                    | `--server-url`       | Omnigent server the bot drives                   |
| `OMNIGENT_SLACK_WEBAUTH_BASE_URL`        | `--webauth-base-url` | This app's public URL — the enrollment link base |
| `OMNIGENT_DATA_DIR`                      | inline               | Ephemeral SQLite store dir                       |
| `DATABRICKS_APP_PORT`                    | Databricks runtime   | Port the enrollment server binds (8000)          |




## Troubleshooting


| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'omnigent_slack'` | The wheel/`pyproject.toml`/`uv.lock` were git-ignored, so `bundle deploy` didn't sync them | Ensure `src/*.whl`, `src/pyproject.toml`, `src/uv.lock` are untracked but NOT git-ignored; re-run `deploy.py` (not `--skip-build` on a clean `src/`) |
| `uv lock` fails with a PyPI DNS error | Public PyPI blocked on the Databricks network | Re-run with `UV_INDEX_URL=https://pypi-proxy.cloud.databricks.com/simple` |
| App install fails; `/logz` shows an `exclude-newer` re-resolve then a PyPI timeout | Runtime's uv `exclude-newer` cutoff differs from the lock's | Read the cutoff from `/logz` and set `_UV_EXCLUDE_NEWER` in `deploy.py` to match, then redeploy |
| `bundle deploy` rejects `user_api_scopes`                             | Databricks CLI < 0.246.0                                                     | Upgrade the CLI                                                                         |
| Enrollment page returns 401 "Could not read your Databricks identity" | User authorization not enabled / not consented                               | Enable it on the app; approve the scope prompt                                          |
| Enrolled, but turns fail auth against the server                      | User lacks access to the server app, or the forwarded token's scopes don't satisfy the server proxy | Grant the user server-app access; widen `user_api_scopes` if the server proxy needs more |
| App boots but Slack shows no sign-in link                             | `--webauth-base-url` not passed (the app URL only exists after first deploy) | Re-deploy with `--webauth-base-url "$(databricks apps get <app> -o json | jq -r .url)"` |
| App can't read secrets                                                | App SP missing scope ACL                                                     | `databricks secrets put-acl <scope> <sp> READ`, redeploy                                |
| Plan shows destroy/replace of the app                                 | `--app-name` mismatch vs. tracked state                                      | Re-check `--app-name`; state is per-app under `root_path`                               |




## Files in this directory


| File | Purpose |
| --- | --- |
| `databricks.yml` | DAB bundle config — app resource, `user_api_scopes`, secrets, env. |
| `deploy.py` | Orchestrator: build wheel → write `pyproject.toml`/`uv.lock` → deploy + run. |
| `src/app.py` | App entry point — runs `omnigent_slack.app.run()`. |
| `src/app.yaml` | App startup config (command + env). |
| `src/*.whl`, `src/pyproject.toml`, `src/uv.lock` | Generated per deploy by `deploy.py`; untracked, not git-ignored. |


