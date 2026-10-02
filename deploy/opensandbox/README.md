# OpenSandbox provider

[OpenSandbox](https://github.com/alibaba/OpenSandbox) is an open-source sandbox
platform with a provider-neutral HTTP API. Omnigent can use an OpenSandbox
deployment as the backend for server-managed agent hosts: each managed session
gets a sandbox running the prebuilt Omnigent host image, and deleting the
session terminates that sandbox.

This integration is managed-only. It does not add OpenSandbox to the
`omnigent sandbox create/connect` CLI bootstrap flow.

## Install and configure

Install the optional SDK and configure the Omnigent server process:

```bash
pip install 'omnigent[opensandbox]'
export OPEN_SANDBOX_API_KEY=...
export OPEN_SANDBOX_DOMAIN=sandbox.example.com
export OPEN_SANDBOX_PROTOCOL=https
```

For deployments whose exec and health endpoints must be resolved through the
OpenSandbox server, also set:

```bash
export OPEN_SANDBOX_USE_SERVER_PROXY=true
```

Then add the [`server-config.example.yaml`](server-config.example.yaml) block to
the Omnigent server config. `sandbox.server_url` must be reachable from inside
the sandbox; it cannot be `localhost`.

By default, OpenSandbox boots
`ghcr.io/omnigent-ai/omnigent-host:latest`, waits up to 300 seconds for a cold
image pull, and requests a 24-hour sandbox lifetime. The managed launch token is
automatically kept valid for one hour longer than the configured lifetime.

Provider options:

| Key | Meaning | Default |
|---|---|---|
| `image` | Registry image with Omnigent preinstalled | official host image |
| `snapshot_id` | OpenSandbox snapshot to restore instead of an image | unset |
| `env` | Server environment variable names copied into the sandbox | none |
| `max_lifetime_s` | Requested sandbox lifetime in seconds | `86400` |
| `ready_timeout_s` | Cold-start readiness timeout in seconds | `300` |

`image` and `snapshot_id` are mutually exclusive. Values in `env` are resolved
from the server process at provision time. Do not put secret values in YAML.
`OPEN_SANDBOX_API_KEY` is a control-plane credential and is explicitly blocked
from passthrough into child sandboxes.

## Images and private registries

The OpenSandbox deployment, not Omnigent, pulls the configured image. Make sure
its runtime can access the registry. For a deployment restricted to a private
registry, mirror the official host image or build the same target:

```bash
docker build -f deploy/docker/Dockerfile --target host \
  -t registry.example.com/omnigent-host:latest .
docker push registry.example.com/omnigent-host:latest
```

Set that reference as `sandbox.opensandbox.image`. Alternatively, prepare an
OpenSandbox snapshot containing the host image and configure `snapshot_id`.

## Model and git credentials

List only the credentials a runner needs, for example:

```yaml
sandbox:
  opensandbox:
    env: [OPENAI_API_KEY, GIT_TOKEN]
```

Prefer scoped, short-lived credentials. OpenSandbox receives these values as
sandbox environment variables. The OpenSandbox API key stays only in the
Omnigent server process.

## Verification

With a server using this provider, run the managed happy-path driver:

```bash
export OMNIGENT_TOKEN=...  # only when the Omnigent API requires authentication
python tests/e2e/integrations/deploy/opensandbox/e2e_managed.py \
  --server https://omnigent.example.com
```

The driver creates a managed session, waits for the OpenSandbox host to
register, waits for a real agent reply, and deletes the session in `finally` so
the server exercises provider cleanup.
