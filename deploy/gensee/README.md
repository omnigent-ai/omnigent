# Omnigent on Gensee

[GenseeAI](https://gensee.ai) provides
[open-source, forkable runtimes with checkpoint/restore support](https://github.com/GenseeAI/gensee-crate)
for managed Omnigent sessions. The built-in launcher talks to the Gensee
control plane over HTTPS; no cloud-provider credentials or Gensee
implementation packages are installed in Omnigent.

Gensee is currently a **managed-host provider only**. Creating a managed
session allocates one sandbox, asks its runtime agent to clone or initialize the
workspace and start `omnigent host`, then waits for that host to connect back.
Deleting or reaping the session releases the sandbox and its workspace data.

It does not implement `omnigent sandbox create`, remote exec/file-copy
primitives, stopped-sandbox resume, or workspace fork/merge capabilities.

## Prerequisites

You need:

1. A Gensee service endpoint and API token. Gensee currently provisions these
   for contracted organizations.
2. An Omnigent server with a public HTTPS URL reachable from the sandbox
   runtime.
3. Any model-provider credentials users need, supplied either by interactive
   login inside their sandbox or through the explicit `env` allowlist below.

Keep the Gensee API token in the server process environment:

```bash
export GENSEE_CONTROLLER_API_TOKEN='<token>'
```

Do not write the token into `config.yaml`.

## Server configuration

The production endpoint and standard token variable are defaults, so the
minimal configuration is:

```yaml
sandbox:
  provider: gensee
  server_url: https://omnigent.example.com
```

Use an explicit block for a dedicated or development endpoint:

```yaml
sandbox:
  provider: gensee
  server_url: https://omnigent.example.com
  gensee:
    endpoint: https://sandbox.gensee.ai
    api_token_env: GENSEE_CONTROLLER_API_TOKEN
    workspace_root: /mnt/gensee-tclone/workspaces
    operation_timeout_s: 900
    poll_interval_s: 2
    request_timeout_s: 40
    retry_timeout_s: 60
    env: []
  reaper:
    enabled: true
    terminate_after_offline_days: 1
    sweep_interval_s: 3600
```

For a multi-provider server, place the same `gensee` block inside its provider
entry:

```yaml
sandbox:
  server_url: https://omnigent.example.com
  providers:
    - provider: gensee
      gensee:
        endpoint: https://sandbox.gensee.ai
    - provider: e2b
```

| Setting | Default | Purpose |
|---|---|---|
| `endpoint` | `https://sandbox.gensee.ai` | Gensee HTTPS control-plane base URL |
| `api_token_env` | `GENSEE_CONTROLLER_API_TOKEN` | Server environment variable holding the API token |
| `workspace_root` | `/mnt/gensee-tclone/workspaces` | Absolute workspace root inside each sandbox |
| `operation_timeout_s` | `900` | Shared deadline for host-start operation submission, retries, and polling; late responses are rejected |
| `poll_interval_s` | `2` | Interval between operation-status requests |
| `request_timeout_s` | `40` | HTTP I/O timeout, capped by the remaining operation budget during host startup |
| `retry_timeout_s` | `60` | Retry budget for transport errors and HTTP 500/502/503/504 responses; `0` disables retries |
| `env` | `[]` | Server environment variable names copied into the sandbox secret |

The endpoint must use HTTPS and cannot contain credentials, a query, or a
fragment. Unknown settings and malformed values stop server startup rather
than failing on the first user session.

`GENSEE_CONTROLLER_URL` overrides the default endpoint when constructing the
launcher directly. An explicit `sandbox.gensee.endpoint` takes precedence in
the server-managed configuration.

## Credential boundary

The Gensee API token stays in the Omnigent server and authorizes lifecycle
requests. Each sandbox receives a separate server-minted Omnigent host token
through Gensee's short-lived secret store; the token is not included in the
durable operation request.

By default, no model or Git credentials are injected. If an operator configures
an allowlist such as:

```yaml
gensee:
  env: [OPENAI_API_KEY, GIT_TOKEN]
```

the corresponding values are read from the Omnigent server environment and
sent to the Gensee secret store. Only list variables that every sandbox on that
server is authorized to receive. For per-user credentials, leave `env` empty
and let each user sign in from their own sandbox terminal.

Public errors include only the controller's error message, with known launch
and API credentials redacted. Raw response bodies are not exposed in session
errors or cleanup logs.

## Lifecycle and cleanup

The launcher uses the following controller operations:

- allocate an ephemeral sandbox;
- store one short-lived launch secret;
- submit and poll `start_omnigent_host`;
- query allocation and runtime-agent readiness;
- release the allocation idempotently.

The Omnigent launch token lasts seven days. Sandbox release is safe to retry,
which lets session deletion and the deployment-wide reaper recover from
temporary control-plane failures. The reaper is disabled unless configured;
see the main [deployment guide](../README.md#run-hosts-in-cloud-sandboxes).

An allocation request can succeed even if its response is lost. The launcher
attempts to release the requested allocation after ambiguous failures. If
cleanup also fails, the error and server log retain the allocation ID so the
operator can locate and release it through the control plane.

## Verify

Start the server, then confirm Gensee is advertised:

```bash
curl -fsS https://omnigent.example.com/v1/info
```

Create a managed session with `sandbox_provider: gensee` in the web UI or API.
The session should progress through provisioning and starting, then its host
should appear online. Delete the test session and confirm the corresponding
allocation is released in the Gensee control plane.

For a repeatable live check, run the opt-in lifecycle E2E against a non-production Omnigent server configured with Gensee. It creates one managed session without an LLM turn, waits for its host and runner, deletes the session, and verifies the exact Gensee allocation is released:

```bash
OMNIGENT_E2E_GENSEE=1 \
OMNIGENT_E2E_GENSEE_SERVER_URL=https://omnigent.example.com \
OMNIGENT_E2E_GENSEE_AGENT_ID=ag_example \
OMNIGENT_E2E_GENSEE_SERVER_TOKEN='<optional-omnigent-bearer-token>' \
GENSEE_CONTROLLER_API_TOKEN='<token>' \
.venv/bin/python -m pytest tests/e2e/integrations/deploy/gensee/test_managed_lifecycle.py -v
```

Omit `OMNIGENT_E2E_GENSEE_SERVER_TOKEN` when the test server has authentication disabled. Set `GENSEE_CONTROLLER_URL` if the server uses a non-default controller endpoint, and set `OMNIGENT_E2E_GENSEE_WORKSPACE_ROOT` if its configured `workspace_root` differs from the default. The test is skipped unless `OMNIGENT_E2E_GENSEE=1` is set and always attempts to delete the session it created.

## Troubleshooting

- **`GENSEE_CONTROLLER_API_TOKEN` must contain the API token** — expose the
  configured token variable to the Omnigent server process or Pod.
- **Controller is not ready** — verify the endpoint's `/readyz` response and
  certificate chain from the Omnigent server host.
- **Operation timed out** — inspect the allocation in the Gensee control plane;
  only increase `operation_timeout_s` after checking sandbox startup and agent
  logs.
- **The sandbox starts but the host never connects** — confirm `server_url` is
  publicly reachable from the sandbox runtime and that its TLS certificate is
  valid.
