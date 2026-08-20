# Omnigent on Tailscale

[Tailscale](https://tailscale.com) gives every device on your network a
stable private hostname (`<machine>.ts.net`) and connects them peer-to-peer
over WireGuard — no port forwarding, no firewall rules. This makes it easy
to access a server running on your laptop from your phone, tablet, or any
other device you own.

> [!NOTE]
> This is not a cloud deploy. Tailscale is a networking layer, not a hosting
> service — you still run the server yourself (laptop, VPS, home server).
> If you want the server to stay up when your laptop closes, deploy to a
> cloud platform (see [../README.md](../README.md)) and use Tailscale just
> for private access.

## Prerequisites

- Tailscale installed on your server machine and every client device.
  All signed in to the same Tailscale account.
- Omnigent server running locally (e.g. `omnigent server` or
  `docker compose up -d` from `deploy/docker/`).

## Tailnet-only access (phone / tablet / remote laptop)

Expose the local server over HTTPS to every device on your tailnet:

```bash
tailscale serve https / http://localhost:8000
```

Tailscale issues a TLS certificate for `https://<machine>.ts.net` and
proxies traffic to `localhost:8000`. No other device on the internet can
reach it.

Set two environment variables on the server before starting it:

```dotenv
# Trust the Tailscale origin so WebSocket handshakes and multipart
# uploads are accepted from the browser on your phone/tablet.
OMNIGENT_WS_ALLOWED_ORIGINS=https://<machine>.ts.net

# Public base URL — used to build the correct __Host- cookie prefix
# and any invite / magic-link URLs.
OMNIGENT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
```

Without `OMNIGENT_WS_ALLOWED_ORIGINS` the browser will get WebSocket close
code `4403` and an HTTP 403 *"Forbidden: this endpoint requires a trusted
Origin header"* on chat and file uploads. Without `OMNIGENT_ACCOUNTS_BASE_URL`
session cookies won't use the `__Host-` prefix and invite links resolve to
the wrong host.

**With Docker Compose** (`deploy/docker/`), add both lines to your `.env`:

```bash
# generate and edit .env if you haven't already
cp deploy/docker/.env.example deploy/docker/.env

# add to .env:
OMNIGENT_WS_ALLOWED_ORIGINS=https://<machine>.ts.net
OMNIGENT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
```

Then restart:

```bash
docker compose up -d
```

Open `https://<machine>.ts.net` on any device on your tailnet.

## Cloud sandbox hosts and Tailscale Funnel

Cloud sandbox providers (Modal, Daytona, E2B, …) run the Omnigent host
process *inside* a remote container. That host dials **out** to
`server_url` over WebSocket to receive work — so it needs to reach the
server from the sandbox provider's cloud network, not just from your
tailnet.

A server behind plain `tailscale serve` is only reachable from your
tailnet. **Tailscale Funnel** fixes this: it makes a specific port
reachable from the public internet while keeping the same
`<machine>.ts.net` hostname.

```bash
tailscale funnel 8000
```

Then point the sandbox config at the public Tailscale URL:

```yaml
# config.yaml (or /data/config.yaml in Docker)
sandbox:
  provider: modal          # or daytona, e2b, …
  server_url: https://<machine>.ts.net
```

> [!IMPORTANT]
> Funnel makes the server reachable from the public internet, so enable
> auth before turning it on:
>
> ```dotenv
> OMNIGENT_AUTH_ENABLED=1
> OMNIGENT_ACCOUNTS_BASE_URL=https://<machine>.ts.net
> ```
>
> See [Auth](../README.md#auth) for the full setup.

## Summary

| Goal | Command | Reachable from |
|---|---|---|
| Access from devices on your tailnet | `tailscale serve https / http://localhost:8000` | Tailnet only |
| Cloud sandbox hosts + tailnet | `tailscale funnel 8000` | Public internet + tailnet |

## Environment variable reference

| Variable | Purpose |
|---|---|
| `OMNIGENT_WS_ALLOWED_ORIGINS` | Comma-separated origin allowlist. Set to `https://<machine>.ts.net` to trust the Tailscale origin for WebSocket and multipart routes. |
| `OMNIGENT_ACCOUNTS_BASE_URL` | Public base URL. Used for session cookie security (`__Host-` prefix) and invite / magic-link URLs. |
| `OMNIGENT_AUTH_ENABLED` | `1` to require login. Recommended when using Tailscale Funnel (public internet exposure). |
