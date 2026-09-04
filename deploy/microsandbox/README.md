# Omnigent on microsandbox

[microsandbox](https://github.com/superradcompany/microsandbox) is an embedded microVM runtime that boots standard OCI images directly as hardware-isolated VMs (libkrun).
Each Omnigent host runs inside its own VM with its own kernel - no shared host kernel, unlike Docker/Podman containers - and boots in well under a second once the image is cached.

The microsandbox provider is fully local and self-hosted: the SDK embeds the runtime in the Omnigent server (or CLI) process.
**No daemon, no server component, no cloud account, no API key.**
Sandbox state lives under `~/.microsandbox` on the machine running Omnigent.

Both integration surfaces are supported:

- **Server-managed** - the server provisions a VM automatically when a session is created with `"host_type": "managed"`, starts `omnigent host` inside it, and removes it when the session is deleted.
- **CLI bootstrap** - `omnigent sandbox create --provider microsandbox` builds your local wheels, boots a VM, overlays the wheels, and attaches, including the in-sandbox App OAuth flow (microsandbox is the first OSS provider with a working local port-forward).

Idle VMs drain themselves (default: after 24h), keeping their writable layer, and are resumed in place on the next use - a drained host restarts in about 100ms.

## Prerequisites

```bash
pip install 'omnigent[microsandbox]'   # installs the microsandbox SDK extra (runtime bundled)
```

Hardware virtualization on the machine running the Omnigent server (or CLI):

- **macOS:** Apple Silicon (Intel Macs are not supported).
- **Linux:** KVM enabled and accessible - `/dev/kvm` must exist and the user must be in the `kvm` group (glibc distros only).

No other install is needed - the Python wheel bundles the runtime and libkrunfw.
The optional `msb` CLI (`brew install superradcompany/tap/microsandbox`) is handy for debugging (`msb doctor`, `msb ls`, `msb logs <name>`).

> **Beta software:** microsandbox is explicitly beta with a breaking-change warning, which is why the extra pins `>=0.6.6,<0.7`.
> Do not bump the minor without re-running the smoke test below.

## Server configuration

Add a `sandbox:` block to your server config (`omnigent server -c ...` / `OMNIGENT_CONFIG` / `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: microsandbox
  server_url: http://host.microsandbox.internal:8799   # see below
```

`provider` + `server_url` is a complete config: the image defaults to the official prebaked host image and VMs run locally.

**`server_url` for a local server:** the in-VM host dials back to the server, and `localhost` inside the VM is the VM itself.
When the Omnigent server runs on the same machine as the VMs (the usual local setup), point `server_url` at `host.microsandbox.internal` (the guest's stable name for its host machine) with the server's port.
A genuinely public `https://...` URL works as-is.

All knobs:

```yaml
sandbox:
  provider: microsandbox
  server_url: http://host.microsandbox.internal:8799
  microsandbox:
    image: ghcr.io/acme/omnigent-host:latest  # optional; default: official image
    env: [OPENAI_API_KEY, GIT_TOKEN]          # optional; SERVER env var NAMES
    cpus: 2                                   # optional; default 2
    memory_mib: 4096                          # optional; default 4096
    idle_timeout_s: 86400                     # optional; default 24h, 0 disables draining
    network: host                             # optional; host (default) | public-only | all
    host_ports: [8317]                        # optional; extra guest-to-host TCP ports
```

### Network modes

| Mode | Guest can reach |
|------|-----------------|
| `host` (default) | Public internet plus selected ports on the host machine (`host.microsandbox.internal`), while loopback, private LANs, and cloud metadata stay blocked. |
| `public-only` | Public internet only, for public `server_url` values that do not need local dial-back or CLI App OAuth port-forwarding. |
| `all` | Everything, including private LANs. |

Under `host` mode, **managed** VMs run untrusted agent code on the same machine as the server, so guest-to-host access is scoped to a TCP port allowlist: the `server_url` port (always) plus any `host_ports` entries.
List the ports of host-local services agents legitimately need - e.g. `host_ports: [8317]` for a local LLM gateway the sandbox env points at via `http://host.microsandbox.internal:8317`.
Everything else on the host stays unreachable.
CLI-bootstrap sandboxes (`omnigent sandbox create`, your own interactive session) keep unrestricted host access, because the App OAuth relay port is not known at VM creation time.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `OMNIGENT_MICROSANDBOX_HOST_IMAGE` | Override the host image (alternative to `sandbox.microsandbox.image`). |
| `OMNIGENT_MICROSANDBOX_SANDBOX_ENV` | Comma-separated SERVER env var names to inject into VMs (alternative to `sandbox.microsandbox.env`). |

The `env` names resolve to their values from the **server's own environment** at provision time - typically the harness LLM credentials (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, gateway base URLs) and `GIT_TOKEN` the in-VM host forwards to runners.
Names only, so secret values never live in the config file.

## CLI bootstrap

```bash
omnigent sandbox create --provider microsandbox --server https://omnigent.example.com
```

Builds the local wheels, provisions a VM from the host image, overlays the wheels, runs the in-sandbox App OAuth flow when the server needs it (the port-forward rides an in-guest relay to `host.microsandbox.internal`, so it needs the default `host` network mode and `python3` in the image - the official host image has it), and holds the host open in the foreground.

## How it works

1. The server provisions a detached VM from the prebaked host image (`Sandbox.create(image=..., detached=True)`), named `<label>-<random>` so repeated creates never collide.
   Detached VMs survive server restarts; any process reconnects by name.
2. The network policy allows public egress plus guest-to-host traffic (the `host` mode above), so the in-VM host can reach `server_url` even when the server is on the same machine.
3. The server runs `omnigent host` inside the VM with a one-time launch token in its environment; the host dials back over a WebSocket tunnel and registers.
   From there the session rides the same host/runner machinery every Omnigent host uses - the agent's runner, tools, and shell all execute inside the VM.
4. After `idle_timeout_s` of inactivity the VM drains: compute and memory are released, the writable layer (workspace, installed tools) is kept.
   The server's wake path resumes it in place (`can_resume`); resume takes about 100ms.
5. On VM death (a crash, or you `msb rm` it), the durable host identity survives and the next message relaunches a fresh VM generation.

Inspect VMs with `msb ls` / `msb logs <name>`; the in-VM host logs to `/tmp/omnigent-host.log`.

## Smoke test

`tests/e2e/integrations/deploy/microsandbox/microsandbox_smoke_test.py` drives the real launcher end to end (provision, exec, file shipping, streaming, port-forward relay, stop/resume, terminate) against a small stock image:

```bash
python tests/e2e/integrations/deploy/microsandbox/microsandbox_smoke_test.py
```

Pass `--image ghcr.io/omnigent-ai/omnigent-host:latest` to smoke the real host image (first pull takes a few minutes).

## Limitations

- **Same-machine only.**
  VMs run where the Omnigent server (or CLI) runs; there is no remote pool mode (unlike boxlite's `cloud:`).
- **Platform floor.** Apple Silicon macOS or KVM glibc Linux; no Intel Macs, no musl/Alpine hosts, Windows support in microsandbox is preview and untested here.
- **`/tmp` is tmpfs.**
  Guest `/tmp` does not survive a drain/resume cycle; the writable layer (everything else, including `$HOME`) does.
- **A server killed mid-provision can orphan a VM.**
  Provisioning runs on a worker thread; if the server process dies at exactly the wrong moment, the created VM is never recorded.
  The idle-drain timeout parks such a VM automatically; `msb ls` / `msb rm` cleans it up.
- **Beta runtime.** See the pin note under Prerequisites.
