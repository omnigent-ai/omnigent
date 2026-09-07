# Blaxel sandbox provider

Run Omnigent hosts in Blaxel sandboxes from your terminal or let the Omnigent server create one for each managed session.

- **CLI-launched:** `omnigent sandbox create` ships your local checkout, then `connect` registers the sandbox as a host.
- **Server-managed:** New Chat or `POST /v1/sessions` creates a sandbox and deletes it with the session.

## Prerequisites

Install the optional Python SDK and the [Blaxel CLI](https://docs.blaxel.ai/cli-reference/introduction). Then log in to your workspace:

```bash
pip install 'omnigent[blaxel]'
brew tap blaxel-ai/blaxel
brew install blaxel
bl login your-workspace
```

The process that launches the sandbox needs Blaxel control credentials. This is your shell for a CLI launch and the server process for a managed launch. Web users log in to Omnigent, not Blaxel.

For a non-interactive server, set the credentials in its environment:

```bash
export BL_WORKSPACE=your-workspace
export BL_API_KEY=your-api-key
```

## CLI-launched sandboxes

Run `create` from an Omnigent checkout. Use a server URL that the Blaxel sandbox can reach:

```bash
omnigent sandbox create \
  --provider blaxel \
  --server https://your-host \
  --name omnigent-dev
```

The command builds wheels from your checkout and overlays them on the standard host image. It prints the sandbox ID when the sandbox is ready.

Register that sandbox as an Omnigent host:

```bash
omnigent sandbox connect \
  --provider blaxel \
  --sandbox-id <id-from-create> \
  --server https://your-host \
  --host-name blaxel-dev
```

`connect` stays open while the host is connected. Press Ctrl-C to stop the connection. Use a unique `--host-name` when you connect more than one sandbox to the same server.

Blaxel does not expose local callback port forwarding. Omnigent therefore skips the in-sandbox browser login automatically. If the host or agent needs environment credentials, list their variable names before `create`:

```bash
export OPENAI_API_KEY=your-openai-key
export OMNIGENT_BLAXEL_SANDBOX_ENV=OPENAI_API_KEY
```

The launcher copies each named value into the sandbox. For a protected CLI target, include the credentials that `omnigent host` uses, such as `DATABRICKS_HOST` and `DATABRICKS_TOKEN`. Never include `BL_API_KEY` or `BL_CLIENT_CREDENTIALS` because Blaxel control credentials must stay outside the sandbox.

Stopping `connect` does not delete the sandbox. Delete it in the Blaxel console or with:

```bash
bl delete sandbox <sandbox-id>
```

## Server-managed sandboxes

Add the Blaxel provider to the server config. `server_url` must be a public URL that Blaxel can reach, not `localhost`:

```yaml
sandbox:
  provider: blaxel
  server_url: https://your-host
  blaxel:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

The `env` list contains names from the server environment, never secret values. See [server-config.example.yaml](server-config.example.yaml) for every provider setting.

In the Web UI, open New Chat and select **Blaxel Sandbox**. The same flow is available through the API:

```bash
curl -X POST https://your-host/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent_...", "host_type": "managed"}'
```

The server provisions the sandbox in the background and shows launch progress in the Web UI. It gives the host a server-minted token for that launch. The user does not need Blaxel credentials. Deleting the session terminates the sandbox and removes its ephemeral file system.

All managed sandboxes use the Blaxel workspace and credentials of the server process. This integration does not map each Omnigent user to a separate Blaxel workspace.

## Image and limits

Omnigent uses `blaxel/omnigent-host:latest` by default. This public Blaxel Hub image combines the standard Omnigent host runtime with Blaxel's `sandbox-api`, which provides process, file, streaming, and lifecycle control. Use `sandbox.blaxel.image` or `OMNIGENT_BLAXEL_HOST_IMAGE` to select a fixed published tag.

| Setting | Default | Purpose |
| --- | --- | --- |
| `image` | `blaxel/omnigent-host:latest` | Host image from Blaxel Hub |
| `env` | empty | Server environment variable names to copy |
| `region` | `BL_REGION` or Blaxel default | Sandbox region |
| `memory_mb` | `4096` | Sandbox memory in MiB |
| `ttl` | `24h` | Provider-side maximum age |

The host uses Blaxel keep-alive mode until the TTL or managed teardown. Process output through the provider API is limited to 4 MiB per command. Omnigent stops commands that cross this limit.

`ttl` is the single knob that sizes a managed session. It is an age from creation, not an idle timeout, so Blaxel deletes the sandbox at that age even while the session is active. The server mints each launch token for that age plus one hour, so the token never outlives the sandbox by more than the reconnect margin. A managed session that must run longer than 24 hours needs a larger `ttl`, for example `7d`. Durations accept `w`, `d`, `h`, `m`, and `s`, alone or combined as `1h30m`.

The Blaxel SDK can send SDK error events to its vendor Sentry endpoint when tracking is enabled in Blaxel configuration. Tracking is off by default. Set `DO_NOT_TRACK=1` on the Omnigent server to disable Blaxel SDK telemetry.

## Run the live smoke test

The smoke test refuses known production workspace names. It creates one unique sandbox and always attempts deletion. Set `OMNIGENT_BLAXEL_HOST_IMAGE` only to test an image override.

```bash
export OMNIGENT_BLAXEL_LIVE_TEST=1
export BL_WORKSPACE=your-non-production-workspace
export OMNIGENT_BLAXEL_TEST_WORKSPACE=your-non-production-workspace
# Optional image override:
# export OMNIGENT_BLAXEL_HOST_IMAGE=blaxel/omnigent-host:<tag>
uv run --extra blaxel python tests/e2e/integrations/deploy/blaxel/blaxel_smoke_test.py
```

The test checks command success and failure, binary file transfer, streaming, active-process cleanup, attach and running-state lookup, and idempotent deletion with final absence.
