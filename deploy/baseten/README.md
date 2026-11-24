# Baseten Sandboxes provider

Run Omnigent hosts in [Baseten Sandboxes](https://docs.baseten.co/sandboxes/overview) from your terminal or let the Omnigent server create one for each managed session.

- **CLI-launched:** `omnigent sandbox create` ships your local checkout, then `connect` registers the sandbox as a host.
- **Server-managed:** New Chat or `POST /v1/sessions` creates a sandbox and deletes it with the session.

## Prerequisites

Install the optional Python SDK:

```bash
pip install 'omnigent[baseten]'
```

The process that launches the sandbox needs a Baseten API key. This is your shell for a CLI launch and the server process for a managed launch. Web users log in to Omnigent, not Baseten.

```bash
export BASETEN_API_KEY=your-api-key
# Needed only when the key belongs to more than one team.
export BASETEN_TEAM_ID=your-team-id
```

## CLI-launched sandboxes

Run `create` from an Omnigent checkout. Use a server URL that the Baseten sandbox can reach:

```bash
omnigent sandbox create \
  --provider baseten \
  --server https://your-host \
  --name omnigent-dev
```

The command builds wheels from your checkout and overlays them on the standard host image. It prints the sandbox ID when the sandbox is ready.

Register that sandbox as an Omnigent host:

```bash
omnigent sandbox connect \
  --provider baseten \
  --sandbox-id <id-from-create> \
  --server https://your-host \
  --host-name baseten-dev
```

`connect` stays open while the host is connected. Press Ctrl-C to stop the connection. Use a unique `--host-name` when you connect more than one sandbox to the same server.

Baseten does not expose local callback port forwarding. Omnigent therefore skips the in-sandbox browser login automatically. If the host or agent needs environment credentials, list their variable names before `create`:

```bash
export OPENAI_API_KEY=your-openai-key
export OMNIGENT_BASETEN_SANDBOX_ENV=OPENAI_API_KEY
```

The launcher copies each named value into the sandbox as a secret environment variable. For a protected CLI target, include the credentials that `omnigent host` uses, such as `DATABRICKS_HOST` and `DATABRICKS_TOKEN`. The launcher refuses `BASETEN_API_KEY` because Baseten credentials must stay outside the sandbox.

Stopping `connect` does not delete the sandbox. Delete it with the SDK:

```bash
python - <<'EOF'
import os
from baseten.sandbox import SandboxClient

client = SandboxClient(
    api_key=os.environ["BASETEN_API_KEY"], team_id=os.environ.get("BASETEN_TEAM_ID")
)
client.delete(name="<sandbox-id>")
EOF
```

## Server-managed sandboxes

Add the Baseten provider to the server config. `server_url` must be a public URL that Baseten can reach, not `localhost`:

```yaml
sandbox:
  provider: baseten
  server_url: https://your-host
  baseten:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

The `env` list contains names from the server environment, never secret values. See [server-config.example.yaml](server-config.example.yaml) for every provider setting.

In the Web UI, open New Chat and select **Baseten Sandbox**. The same flow is available through the API:

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

The server provisions the sandbox in the background and shows launch progress in the Web UI. It gives the host a server-minted token for that launch. The user does not need Baseten credentials. Deleting the session deletes the sandbox and its file system.

All managed sandboxes use the Baseten team and API key of the server process. This integration does not map each Omnigent user to a separate Baseten team.

## Image and limits

By default, Omnigent builds the public `ghcr.io/omnigent-ai/omnigent-host` image into your Baseten team as `omnigent-host-<tag>` the first time it launches a sandbox. The tag matches the installed Omnigent release, or `latest` for development builds. Baseten adds its sandbox API to the image, which provides process, file, streaming, and lifecycle control. The first build takes a few minutes; later launches reuse the built image. A `latest` build is not refreshed when the upstream image moves: delete the `omnigent-host-latest` image from the team, or set `image`, to pick up a newer one.

Use `sandbox.baseten.image` or `OMNIGENT_BASETEN_HOST_IMAGE` to select an image you have already built in the team, such as `my-omnigent-host:latest`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `image` | `omnigent-host-<tag>:latest`, built on first use | Baseten image to boot |
| `env` | empty | Server environment variable names to copy |
| `region` | `OMNIGENT_BASETEN_REGION` or the closest region | Sandbox region |
| `memory_mb` | `4096` | Sandbox memory in MiB, which also sets the CPU allocation |
| `ttl` | `24h` | Provider-side maximum age |

The host runs as a keep-alive process, so Baseten keeps the sandbox active until the TTL or managed teardown. Process output through the provider API is limited to 4 MiB per command. Omnigent stops commands that cross this limit. Baseten has no sandbox pause or snapshot, so a stopped sandbox cannot resume; relaunching a managed host creates a new sandbox.

`ttl` sets the sandbox's age limit from creation, not an idle timeout, so Baseten deletes the sandbox at that age even while the session is active. The default host token window is that age plus one hour; [`sandbox.token_ttl_s`](../README.md#run-hosts-in-cloud-sandboxes) overrides the token window independently. Connected hosts renew the same token halfway through its window. A managed session that must run longer than 24 hours needs a larger `ttl`, for example `7d`. Durations accept `w`, `d`, `h`, `m`, and `s`, alone or combined as `1h30m`; Baseten's shortest age is `5m`.

## Run the live smoke test

Use a non-production team. The test creates one uniquely named sandbox with a one-hour maximum age and always deletes it. Set `OMNIGENT_BASETEN_HOST_IMAGE` only to test an image override.

```bash
export OMNIGENT_BASETEN_LIVE_TEST=1
export BASETEN_API_KEY=your-api-key
export BASETEN_TEAM_ID=your-non-production-team-id
export OMNIGENT_BASETEN_TEST_TEAM=your-non-production-team-id
uv run --extra baseten python tests/e2e/integrations/deploy/baseten/baseten_smoke_test.py
```

The test checks command success and failure, binary file transfer, streaming, active-process cleanup, attach and running-state lookup, and idempotent deletion with final absence.
