# CoreWeave Sandbox provider

Run Omnigent hosts in [CoreWeave Sandbox](https://docs.coreweave.com/products/sandboxes)
sandboxes. The launcher wraps the official
[`cwsandbox`](https://github.com/coreweave/cwsandbox-client) Python SDK, gated
behind the `cwsandbox` extra and imported lazily — same posture as the Modal
and Daytona launchers.

```bash
pip install 'omnigent[cwsandbox]'
```

## Credentials

Set on the **server** process (12-factor; never in config files):

```bash
export CWSANDBOX_API_KEY=...                          # CoreWeave Sandbox API key
export CWSANDBOX_BASE_URL=https://api.cwsandbox.com   # optional (this is the default)
```

## Server config

```yaml
sandbox:
  provider: cwsandbox
  server_url: https://omnigent.example.com   # public URL the sandbox host dials back to
  cwsandbox:                                  # optional
    image: ghcr.io/my-org/omnigent-host:latest  # default: official host image
    env: [ANTHROPIC_API_KEY, GIT_TOKEN]          # server env var NAMES injected into the sandbox
```

`provider` + `server_url` is a complete config. `server_url` **must be
reachable from CoreWeave** — the host inside the sandbox opens an outbound
WebSocket to it. For local testing, expose your server with a tunnel
(`cloudflared` / `ngrok`) and point `server_url` at the tunnel URL.

Optional env overrides: `OMNIGENT_CWSANDBOX_HOST_IMAGE`,
`OMNIGENT_CWSANDBOX_SANDBOX_ENV` (comma-separated names),
`OMNIGENT_CWSANDBOX_MAX_LIFETIME_S` (default 24h).

Both server-managed sessions (`host_type="managed"`) and the CLI bootstrap
(`omnigent sandbox create --provider cwsandbox`) are supported. The Databricks
in-sandbox OAuth flow does not apply (CW Sandbox has no inbound port forward),
so `omnigent sandbox create` auto-skips it — fine for token/OIDC-auth servers.

## Notes / limits

- Sandboxes are reaped at `max_lifetime_seconds` (24h default; override with
  `OMNIGENT_CWSANDBOX_MAX_LIFETIME_S`). The managed launch token TTL is set
  above that so reconnects keep working.
- Egress defaults to none on CW Sandbox; the launcher requests
  `egress_mode: internet` so the host can reach the server.

## Smoke test

Validate the API primitives directly (no Omnigent or SDK install needed —
stdlib + curl only):

```bash
export CWSANDBOX_API_KEY=...
python tests/e2e/integrations/deploy/cwsandbox/smoke_test.py
```
