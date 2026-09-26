# Omnigent on Apache Mesos/ClusterD

The `mesos` sandbox provider runs Omnigent managed hosts through a running
[mesos-compose](https://github.com/AVENTER-UG/mesos-compose) framework. The
Omnigent server submits compose service per managed session; mesos-compose
translates that service into a Mesos task.

This provider is **server-managed only**. It does not implement
`omnigent sandbox create`, `omnigent sandbox connect`, or local port forwarding.
Create a session with `host_type: "managed"` after configuring the server.

## Prerequisites

- An Apache Mesos/ClusterD cluster supported by your mesos-compose deployment.
- A reachable mesos-compose HTTP API exposing `/api/compose/v0`.
- A server URL reachable from the Mesos task. The host inside the task opens an
  outbound WebSocket to this URL; `localhost` refers to the task, not the
  Omnigent server.
- A host image containing Omnigent and the required harnesses. By default the
  launcher uses `ghcr.io/omnigent-ai/omnigent-host:latest`.
- The server installation with Omnigent available. No additional Python extra
  is required for the Mesos launcher; its HTTP client is part of the base
  installation.

For local development, expose the server with a tunnel or use a routable
internal DNS name. Do not use a URL that is reachable only from the server
process itself.

## Server configuration

Omnigent server configuration is YAML. Add a `sandbox:` block to the file
passed to `omnigent server -c config.yaml` (or to the server's configured data
 directory):

```yaml
sandbox:
  provider: mesos
  server_url: https://omnigent.example.test
  mesos:
    image: ghcr.io/example/omnigent-host:latest
    env: [OPENAI_API_KEY, GIT_TOKEN]
    compose_url: https://mesos-compose.example.test:10002
    username: compose-user
    verify_ssl: true
    target_hostname: mesos-agent-01.example.test
```

`compose_url` is required unless `OMNIGENT_MESOS_COMPOSE_URL` is set. The
password is never read from this YAML file; set
`OMNIGENT_MESOS_COMPOSE_PASSWORD` in the server environment instead.

### Configuration keys

| Key | Meaning |
|---|---|
| `provider` | Must be `mesos`. |
| `server_url` | Public or cluster-routable URL used by the in-task `omnigent host` process to dial back. |
| `mesos.image` | Optional container image. Defaults to the official Omnigent host image. |
| `mesos.env` | Optional list of **server environment variable names** copied into the task. Values are not stored in the YAML config. |
| `mesos.compose_url` | Required mesos-compose base URL, including `http://` or `https://`. |
| `mesos.username` | Optional Basic-auth username. The password comes from `OMNIGENT_MESOS_COMPOSE_PASSWORD`. |
| `mesos.verify_ssl` | Optional TLS certificate verification switch; defaults to `true`. Keep it enabled in production. |
| `mesos.target_hostname` | Optional Mesos placement constraint, rendered as `node.hostname==<value>`. |
| `sandbox.host_config` | Optional provider-agnostic in-sandbox Omnigent config, written before `omnigent host` starts. Use `api_key_ref: env:NAME` for secrets. |

A managed session can then be created through the API or the web UI:

```bash
curl -X POST https://omnigent.example.test/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"agent_example","host_type":"managed"}'
```

Provisioning happens in the background. The session becomes runnable after the
Mesos task reaches `TASK_RUNNING`. Deleting the managed session terminates the
mesos-compose service.

## TOML operator profile

Omnigent itself reads the YAML configuration shown above; it does **not** parse
TOML for the Mesos provider. If your deployment tooling stores environment
profiles as TOML, the following is a suitable example for that tooling to
translate into the YAML and environment variables above:

```toml
[server]
url = "https://omnigent.example.test"

[sandbox]
provider = "mesos"
server_url = "https://omnigent.example.test"
image = "ghcr.io/example/omnigent-host:latest"
compose_url = "https://mesos-compose.example.test:10002"
username = "compose-user"
verify_ssl = true
target_hostname = "mesos-agent-01.example.test"
env = ["OPENAI_API_KEY", "GIT_TOKEN"]

[environment]
OMNIGENT_MESOS_COMPOSE_PASSWORD = "set-out-of-band"
```

Do not commit real passwords or API keys. A production wrapper should resolve
`OMNIGENT_MESOS_COMPOSE_PASSWORD` from a secret store rather than putting the
value in a TOML file.

## Authentication and TLS

- With no username configured, mesos-compose requests are unauthenticated.
- When `username` is configured, `OMNIGENT_MESOS_COMPOSE_PASSWORD` must also be
  set and the endpoint must use HTTPS. Basic authentication is rejected over
  plain HTTP.
- `verify_ssl` defaults to `true`. Disabling verification with authenticated
  requests additionally requires `OMNIGENT_MESOS_ALLOW_INSECURE_TLS=1`; this is
  intended only for controlled development environments.
- The server verifies that `GET /api/compose/versions` advertises
  `/api/compose/v0` before launching a host.

## Task lifecycle

For each managed host, the launcher:

1. Generates a unique project name.
2. Builds a Compose document with one service named `host`.
3. Submits it with `PUT /api/compose/v0/<project>` using `application/yaml`.
4. Polls `GET /api/compose/v0/tasks` until the matching task is
   `TASK_RUNNING` or enters a terminal state.
5. Deletes the host service with
   `DELETE /api/compose/v0/<project>/host` during cleanup, retrying transient
   failures.

The generated service uses the configured image, runs `omnigent host`, sets the
launch token and host identity in its environment, requests Docker execution,
and defaults to one CPU and 1024 MiB memory. If `target_hostname` is set, the
service receives the corresponding Mesos node constraint.

A repository workspace, when requested by the managed-session flow, is cloned
inside `/mnt/mesos/sandbox/workspace`. The host process runs with
`/mnt/mesos/sandbox` as `HOME`.

## Environment variables

| Variable | Purpose |
|---|---|
| `OMNIGENT_MESOS_HOST_IMAGE` | Overrides the default host image when `mesos.image` is not configured. |
| `OMNIGENT_MESOS_COMPOSE_URL` | Supplies `mesos.compose_url` when it is not in YAML. |
| `OMNIGENT_MESOS_COMPOSE_USERNAME` | Supplies the Basic-auth username when it is not in YAML. |
| `OMNIGENT_MESOS_COMPOSE_PASSWORD` | Basic-auth password; required when a username is configured. |
| `OMNIGENT_MESOS_TARGET_HOSTNAME` | Supplies the placement hostname when it is not in YAML. |
| `OMNIGENT_MESOS_SANDBOX_ENV` | Comma-separated server environment variable names to pass into the task when `mesos.env` is omitted. |
| `OMNIGENT_MESOS_ALLOW_INSECURE_TLS` | Must be `1`, `true`, or `yes` to combine authenticated requests with `verify_ssl: false`. |

The passthrough list contains names, not literal values. Every listed variable
must exist in the server process environment. Reserved Omnigent identity
variables cannot be overridden.

## Security notes

- Keep the mesos-compose endpoint on HTTPS and leave certificate verification
  enabled. If Basic auth is used, never place its password in the repository,
  YAML, or TOML examples.
- Pass only the model, gateway, and Git variables needed by the host. Use
  short-lived or least-privilege credentials where possible.
- `server_url` must be reachable by the Mesos task but should not expose more
  of the server than required. Use network policy and a reverse proxy where
  appropriate.
- Mesos placement is an operational constraint, not an isolation boundary.
  Apply the cluster's normal task, network, and image policies to the runner
  agents.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `mesos-compose URL is required` | No URL in YAML or environment. | Set `sandbox.mesos.compose_url` or `OMNIGENT_MESOS_COMPOSE_URL`. |
| Authentication fails with HTTP 401 | Wrong username/password or missing password. | Set the username in YAML or env and the password only in `OMNIGENT_MESOS_COMPOSE_PASSWORD`. |
| Basic authentication is rejected | The compose endpoint uses HTTP. | Use an HTTPS endpoint. |
| API version check fails | The endpoint is not mesos-compose or does not advertise v0. | Check `GET /api/compose/versions` and use a compatible mesos-compose deployment. |
| Task never reaches `TASK_RUNNING` | Image pull, placement, resource, or Mesos scheduling failure. | Inspect mesos-compose and Mesos task state; verify the image, resources, and `target_hostname`. |
| Host starts but never registers | `server_url` is not reachable from the task. | Test DNS, routing, TLS, and firewall rules from a Mesos agent. |
| Task exits immediately | Host image or startup command failure. | Inspect the task logs and verify the image contains `python3` and `omnigent`. |

## Limitations

- Server-managed sessions are supported; CLI bootstrap and local port forwarding
  are not.
- Resume of a stopped Mesos task is not supported. A failed task is relaunched
  through the managed-session lifecycle when the server requests a new
  generation.
- The launcher relies on mesos-compose for offer handling, reconciliation,
  task state, and cleanup; it does not implement a Mesos scheduler directly.
