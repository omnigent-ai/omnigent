# Omnigent on BoxLite

[BoxLite](https://github.com/boxlite-ai/boxlite) is an embeddable
micro-VM + OCI runtime ("SQLite for sandboxing"). It runs each Omnigent
host inside its own lightweight VM (its own kernel — KVM on Linux,
Hypervisor.framework on macOS) booted from a standard OCI image.

The boxlite provider is **server-managed only**: the server provisions a
box automatically when a session is created with `"host_type":
"managed"`, starts `omnigent host` inside it, and removes it when the
session is deleted. (There is no `omnigent sandbox create` CLI bootstrap
for boxlite yet — see [Limitations](#limitations).)

A single `boxlite` provider spans **both** runtime targets, chosen by
config:

- **Local** (default — no `cloud:` block): BoxLite is embedded in the
  Omnigent server process. **No daemon, no `boxlite serve`, no root.**
  Boxes are micro-VMs on the server host itself, so that host needs
  hardware virtualization. The first local, hardware-isolated,
  persistent runner — no cloud account required.
- **Cloud** (a `cloud:` block with `endpoint`): a thin REST client to a
  remote `boxlite serve` pool. Boxes run on the pool; the server reaches
  them over HTTP. Same role as the Modal / Daytona providers, self-hosted.

The two modes are configured by mutually-exclusive `local:` / `cloud:`
sub-blocks (see [Server configuration](#server-configuration)).

Boxes boot from the official prebaked host image, so startup is seconds
once the image is cached locally (the first boot from a given image
pulls it, which can take a few minutes).

## Prerequisites

```bash
pip install 'omnigent[boxlite]'   # installs the boxlite SDK extra
```

**Local mode** additionally needs hardware virtualization on the
*server host*:

- **Linux:** KVM enabled and accessible — `/dev/kvm` must exist and the
  server user must be in the `kvm` group.
- **macOS (Apple Silicon):** Hypervisor.framework, always available.

**Cloud mode** needs a reachable `boxlite serve` endpoint; the server
host needs no virtualization.

## Server configuration

Add a `sandbox:` block to your server config (`omnigent server -c …` /
`OMNIGENT_CONFIG` / `<data_dir>/config.yaml`).

### Local micro-VMs (no cloud account)

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnigent.example.com   # the in-box host dials this back
```

`provider` + `server_url` is a complete config: the image defaults to
the official prebaked host image and boxes run locally.

### Cloud (remote `boxlite serve` pool)

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnigent.example.com
  boxlite:
    image: docker.io/me/omnigent-host:latest     # optional, shared; default: official
    env: [OPENAI_API_KEY, GIT_TOKEN]             # optional, shared; SERVER env var NAMES
    cloud:
      endpoint: https://boxlite.example.com:8100 # selects CLOUD mode
```

`local:` and `cloud:` are **mutually exclusive** — a session runs in exactly
one mode. Provider credentials are **not** in this file (12-factor): in cloud
mode the API key is read from `BOXLITE_API_KEY` in the server environment.

### Local runtime customization (data dir, private host image)

Local mode embeds the boxlite runtime, so you can point it at a specific
data directory and give it credentials to pull a **private** host image
(the local analog of the cloud providers' registry secrets):

```yaml
sandbox:
  provider: boxlite
  server_url: https://omnigent.example.com
  boxlite:
    image: ghcr.io/acme/omnigent-host:latest   # shared
    local:                           # LOCAL mode block (mutually exclusive with `cloud`)
      home_dir: /data/boxlite        # runtime state + image cache (default ~/.boxlite)
      registry:
        host: ghcr.io
        username_env: GHCR_USER      # NAME of a server env var (not the value)
        password_env: GHCR_PAT
        # token_env: GHCR_TOKEN      # bearer-token alternative
        # transport: https           # or "http"
        # skip_verify: false
```

The `local:` block applies to local mode only and is mutually exclusive with
`cloud:`. When `local:` is omitted (or empty) the launcher uses the zero-config
`Boxlite.default()` runtime. Registry credentials are read from the named server
env vars at provision time — values never live in the config file.

> **Security:** `transport: https` (the default) and `skip_verify: false` keep
> the registry pull encrypted and certificate-verified. `transport: http` sends
> the pull credentials in **cleartext**, and `skip_verify: true` disables TLS
> verification — use them only on a trusted local network. Likewise, a cloud
> `endpoint` with an `http://` scheme ships `BOXLITE_API_KEY` in cleartext;
> prefer `https://`.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `BOXLITE_API_KEY` | API key for the remote `boxlite serve` (cloud mode only). |
| `OMNIGENT_BOXLITE_HOST_IMAGE` | Override the host image (alternative to `sandbox.boxlite.image`). |
| `OMNIGENT_BOXLITE_SANDBOX_ENV` | Comma-separated SERVER env var names to inject into boxes (alternative to `sandbox.boxlite.env`). |

The `env` names resolve to their values from the **server's own
environment** at provision time — typically the harness LLM credentials
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, gateway base URLs) and
`GIT_TOKEN` the in-box host forwards to runners. Names only, so secret
values never live in the config file.

## How it works

1. The server provisions a box from the prebaked host image
   (`runtime.create(BoxOptions(image=…, auto_remove=False))`). Boxes are
   persistent — the managed-session machinery owns teardown.
2. Network defaults to full egress, so the in-box host can reach
   `server_url`.
3. The server runs `omnigent host` inside the box (over `box.exec`) with
   a one-time launch token in its environment; the host dials back over
   a WebSocket tunnel and registers. From there the session rides the
   same host/runner machinery every Omnigent host uses — the agent's
   runner, tools, and shell all execute inside the box.
4. On sandbox death (a crash, or you `boxlite rm` it), the durable host
   identity survives and the next message relaunches a fresh box
   generation.

Inspect running boxes with the CLI (`boxlite list`, `boxlite logs <id>`);
the in-box host logs to `/tmp/omnigent-host.log`.

## Limitations

- **Managed-only.** The `omnigent sandbox create` / `connect` CLI
  bootstrap (local wheel shipping + in-sandbox App OAuth) is not
  implemented for boxlite. Use the server-managed flow above. (Adding
  CLI bootstrap later is straightforward — the async `Box.copy_into`
  supports file shipping; the sync SDK wrapper does not, which is why
  the launcher uses the async API.)
- **Network policy.** Boxes get full outbound egress by default. If your
  deployment needs an allowlist, that's a follow-up on `BoxOptions`'
  network spec.
