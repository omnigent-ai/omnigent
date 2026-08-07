# Blaxel sandbox provider

Omnigent uses `blaxel/omnigent-host:latest` by default for managed Blaxel hosts. The public image combines the standard Omnigent host runtime with Blaxel's `sandbox-api`, which supplies the process, file, streaming, and lifecycle control used by the provider. The public reference tracks the current Hub release. Use a fixed Blaxel image tag only when a production rollout must stay on fixed image contents.

## Configure the server

Install the optional Python SDK plus the separate [Blaxel CLI](https://github.com/blaxel-ai/toolkit#installation). On macOS:

```bash
pip install 'omnigent[blaxel]'
brew tap blaxel-ai/blaxel
brew install blaxel
bl login your-non-production-workspace
```

The server needs Blaxel control credentials. The sandbox does not need them.

```bash
export BL_WORKSPACE=your-non-production-workspace
export BL_API_KEY=your-api-key
```

Use [server-config.example.yaml](server-config.example.yaml) for a managed-host deployment. `sandbox.blaxel.image` is optional and overrides the standard image, for example with a fixed published tag. `OMNIGENT_BLAXEL_HOST_IMAGE` provides the equivalent environment override.

The `sandbox.blaxel.env` list contains server environment variable names. It must not contain secret values. Do not add `BL_API_KEY` or `BL_CLIENT_CREDENTIALS` to that list.

The host connects out to `sandbox.server_url`. The Blaxel SDK does not expose local-to-sandbox port forwarding. The CLI therefore supports `--no-auth`, but it cannot open the remote host UI on a local forwarded port.

The host process uses Blaxel keep-alive mode. Managed teardown deletes the sandbox and its ephemeral file system. When `ttl` is omitted, Omnigent applies a 24-hour provider-side maximum age. Set `ttl` explicitly to choose another bounded lifetime. Process output consumed through the provider API is limited to 4 MiB per command. Omnigent kills a command that crosses the bound, which prevents sandbox-api logs from exhausting server memory.

The Blaxel SDK can send SDK-error events to its vendor Sentry endpoint when tracking is enabled in Blaxel configuration. Tracking is opt-in by default. Set `DO_NOT_TRACK=1` on the Omnigent server to disable both Sentry error reporting and other Blaxel SDK telemetry.

## Run the live smoke test

The smoke test refuses known production workspace names. It creates one unique sandbox and always attempts deletion. Set `OMNIGENT_BLAXEL_HOST_IMAGE` only to override the standard image.

```bash
export OMNIGENT_BLAXEL_LIVE_TEST=1
export BL_WORKSPACE=your-non-production-workspace
export OMNIGENT_BLAXEL_TEST_WORKSPACE=your-non-production-workspace
# Optional image override:
# export OMNIGENT_BLAXEL_HOST_IMAGE=blaxel/omnigent-host:<tag>
uv run --extra blaxel python tests/e2e/integrations/deploy/blaxel/smoke_test.py
```

The test checks command success and failure, binary file transfer, streaming, active-process cleanup, attach and running-state lookup, and idempotent deletion with terminal absence.
