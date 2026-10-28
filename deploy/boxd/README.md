# boxd sandbox provider

Run Omnigent hosts on [boxd](https://boxd.sh) machines: hosted KVM microVMs,
each a full Ubuntu computer with its own persistent disk, that boot in about
a second, sleep in place and wake in milliseconds. This is a sandbox-provider
guide, not a server deploy target.

boxd is not [Boxlite](../boxlite/README.md). Boxlite runs micro-VMs on the
Omnigent server host itself (or a self-hosted `boxlite serve` pool); boxd is a
managed cloud service reached over its public API with an API key.

## Prerequisites

```bash
pip install 'omnigent[boxd]'
boxd auth keys create omnigent --org <your-org>   # or create a key at https://app.boxd.sh
export BOXD_API_KEY=bxd_...
export BOXD_ORG=<your-org>                         # optional; defaults to the key's org
```

The launcher also accepts `BOXD_TOKEN`. Credentials stay in the server (or
CLI) environment and never enter a machine.

## Server-managed sandboxes

```yaml
sandbox:
  provider: boxd
  server_url: https://your-host    # public URL machines dial back to
  boxd:                            # every field is optional
    env: [ANTHROPIC_API_KEY, GIT_TOKEN]
    org: my-org
    vcpu: 2
    snapshot: omnigent-host
    omnigent_spec: omnigent==0.19.1
```

| Field | Default | Meaning |
| --- | --- | --- |
| `env` | none (`OMNIGENT_BOXD_SANDBOX_ENV`) | Server environment variable NAMES whose values are sent with every command in the machine, including the host process. Use it for harness credentials and `GIT_TOKEN`. |
| `org` | `BOXD_ORG`, then the key's org | boxd organization machines are created in. |
| `vcpu` | the org's default size | `1`, `2`, `4` or `8` (4, 8, 16 or 32 GiB of memory). Not allowed with `snapshot`. |
| `snapshot` | none (`OMNIGENT_BOXD_SNAPSHOT`) | boxd snapshot to create machines from; see [Faster launches](#faster-launches-with-a-snapshot). |
| `omnigent_spec` | the server's own release | pip requirement a fresh machine installs the host from. A development server installs the latest release. |

Creating a session with `"host_type": "managed"` (or picking **boxd Sandbox**
in the web UI) then:

1. creates an isolated machine named `omnigent-<host name>-<suffix>` with
   auto-suspend off,
2. installs what the official host image bakes and boxd's base image lacks:
   `omnigent` (with `uv tool install`), `bubblewrap`, and the `GIT_TOKEN`
   credential helper (about 20 s; skipped on a prepared snapshot),
3. clones the requested repositories into `~/workspace` and starts
   `omnigent host` with a server-minted launch token.

Deleting the session deletes the machine. boxd's base image already has git,
tmux, Node, uv, Docker, Claude Code and Codex.

### Isolation

Every machine is created with boxd's `isolated` flag: no in-machine `boxd` CLI,
metadata endpoint, integrations or agent logins, so code an agent runs cannot
act on your boxd account. Machines keep outbound internet access.

Every boxd machine also has a public HTTPS address, `https://<machine>.boxd.sh`.
A server an agent starts on port 80, 443, 3000, 5173, 8000 or 8080 is reachable
there, behind boxd's bot protection.

### Sleep and resume

Machines have no lifetime cap. While the host is connected, its tunnel traffic
keeps the machine running. A machine whose host has gone quiet hibernates
after boxd's network-idle timeout (4 hours by default), keeping its disk and
memory. The next message to the session wakes it in place: hibernated and
standby machines resume, a stopped machine starts again, and the workspace is
still there. The web UI shows such a session as asleep rather than offline.

## Faster launches with a snapshot

A fresh machine spends most of its launch installing the host. Bake those
dependencies into a boxd snapshot once and every launch restores it instead:

```bash
boxd machine new omnigent-host-base --isolated
boxd machine exec omnigent-host-base -- \
  'uv tool install --python 3.12 omnigent==<version> &&
   sudo apt-get update -qq && sudo apt-get install -y -qq bubblewrap'
boxd snapshots save omnigent-host-base omnigent-host
boxd machine remove omnigent-host-base --confirm
```

Then set `snapshot: omnigent-host`. Re-save the snapshot when you upgrade the
server. A machine keeps its snapshot's size, so `vcpu` cannot be combined with
`snapshot`.

## CLI-launched sandboxes

```bash
OMNIGENT_BOXD_SANDBOX_ENV=ANTHROPIC_API_KEY omnigent sandbox create --provider boxd --server https://your-host
omnigent sandbox connect --provider boxd --sandbox-id <id> --server https://your-host
```

`create` provisions a machine and installs wheels built from your local
checkout; `connect` runs `omnigent host` in it and holds it open until Ctrl-C,
which stops the remote process. boxd has no local-to-machine port forward, so
the in-sandbox `omnigent login` step is skipped: use servers that do not need
in-sandbox App auth. Delete the machine with `boxd machine remove <name>`.

## Live tests

Both create billable machines and delete them afterwards:

```bash
OMNIGENT_E2E_BOXD=1 BOXD_API_KEY=bxd_... \
  .venv/bin/python -m pytest tests/e2e/integrations/deploy/boxd/test_launcher_live.py -v

OMNIGENT_E2E_BOXD=1 OMNIGENT_E2E_BOXD_SERVER_URL=https://your-host \
  OMNIGENT_E2E_BOXD_AGENT_ID=<agent id> BOXD_API_KEY=bxd_... \
  [OMNIGENT_E2E_BOXD_SERVER_TOKEN=...] [OMNIGENT_E2E_BOXD_PROMPT=1] \
  .venv/bin/python -m pytest tests/e2e/integrations/deploy/boxd/test_managed_lifecycle.py -v
```

## Troubleshooting

- `boxd machine list` shows the machines; managed ones start with `omnigent-`.
- The host logs to `/tmp/omnigent-host.log`:
  `boxd machine exec <machine> -- tail -50 /tmp/omnigent-host.log`.
- A failed bootstrap prints the apt log and deletes the machine.
