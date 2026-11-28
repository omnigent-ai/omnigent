# kosa8 sandbox provider

Run Omnigent hosts in [kosa8](https://kosa8.com) Cloud sandboxes: hosted
microVMs, each with its own kernel, that boot straight from an OCI image,
sleep with their memory saved and wake in place under the same id. This is a
sandbox-provider guide, not a server deploy target.

The launcher talks to kosa8's REST API with the HTTP client Omnigent already
ships, so there is no extra to install.

## Prerequisites

```bash
export KOSA8_API_KEY=...                       # create one at https://app.kosa8.com
export KOSA8_API_URL=https://api.kosa8.com     # optional; the default
```

The key stays in the server (or CLI) environment and never enters a sandbox.

## Server-managed sandboxes

```yaml
sandbox:
  provider: kosa8
  server_url: https://your-host    # public URL sandboxes dial back to
  kosa8:                           # every field is optional
    env: [ANTHROPIC_API_KEY, GIT_TOKEN]
    image: ghcr.io/omnigent-ai/omnigent-host:latest
    size: 4x8
    region: eu
    labels: {team: agents}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `env` | none (`OMNIGENT_KOSA8_SANDBOX_ENV`) | Server environment variable NAMES whose values are set on the sandbox when it is created. kosa8 keeps them on the sandbox's host, gives them to every command, including the host process, and never logs or returns them. At most 64. |
| `image` | the official host image (`OMNIGENT_KOSA8_HOST_IMAGE`) | OCI image the sandbox boots. Build custom ones from the `host` target of [`docker/Dockerfile`](../docker/Dockerfile). |
| `size` | `2x4` | vCPU x GiB: `2x4`, `4x8`, `8x16` or `16x32`. |
| `region` | the org's default | kosa8 region, e.g. `us`, `eu`, `us-ash1` or `eu-hel1`. |
| `snapshot` | none | kosa8 snapshot (`snap_...`) to create sandboxes from; see [Faster launches](#faster-launches-with-a-snapshot). Not allowed with `image`, `size` or `region`. |
| `labels` | none | kosa8 labels added to every sandbox, next to `created-by=omnigent`. |

Creating a session with `"host_type": "managed"` (or picking **kosa8 Sandbox**
in the web UI) then:

1. creates a sandbox named `omnigent-<host name>-<suffix>` from the host
   image, labelled `created-by=omnigent`, with idle sleep off,
2. waits until it runs and accepts commands (a cold image boot takes longer
   the first time a host pulls the image),
3. clones the requested repositories and starts `omnigent host` with a
   server-minted launch token.

Deleting the session deletes the sandbox.

Sandboxes are created `persistent`: every command runs in one long-lived
container, so `$HOME`, the host config and the background `omnigent host`
last between commands. kosa8 plans cap how many persistent sandboxes an org
can have at once, so that cap is also the number of managed hosts.

### Sleep and wake

Sandboxes have no lifetime cap. When the managed-session machinery parks a
host, the launcher sleeps its sandbox: kosa8 saves memory, processes and disk
and frees the RAM, so a sleeping sandbox bills snapshot storage only. The
next message to the session wakes it in place with the workspace intact.

A woken sandbox still runs its old `omnigent host` on a launch token the
server has revoked. The launcher stops it before it starts a host with a fresh
token, so there is only ever one.

### Commands

Every command runs over kosa8's streamed exec, which has no time limit of its
own, so long clones and installs finish. kosa8's exec has no PTY: the
foreground `connect` streams output line by line.

## Faster launches with a snapshot

A cold boot pulls the host image onto a kosa8 host. Save a sandbox the
launcher created as a snapshot once, and every launch restores it instead:

```bash
curl -fsS -X POST -H "Authorization: Bearer $KOSA8_API_KEY" \
  https://api.kosa8.com/v1/sandboxes/<sandbox id>/snapshots
```

Then set `snapshot: snap_...`. A sandbox made from a snapshot keeps the
snapshot's image, size and region, so those fields cannot be combined with it.

## CLI-launched sandboxes

```bash
OMNIGENT_KOSA8_SANDBOX_ENV=ANTHROPIC_API_KEY omnigent sandbox create --provider kosa8 --server https://your-host
omnigent sandbox connect --provider kosa8 --sandbox-id <id> --server https://your-host
```

`create` provisions a sandbox and installs wheels built from your local
checkout; `connect` runs `omnigent host` in it and holds it open until Ctrl-C,
which stops the remote process. kosa8 has no local-to-sandbox port forward, so
the in-sandbox `omnigent login` step is skipped: use servers that do not need
in-sandbox App auth.

## Live tests

The test creates billable sandboxes and deletes them afterwards:

```bash
OMNIGENT_E2E_KOSA8=1 KOSA8_API_KEY=... \
  .venv/bin/python -m pytest tests/e2e/integrations/deploy/kosa8/test_kosa8_launcher_live.py -v
```

## Troubleshooting

- List the sandboxes Omnigent created:
  `curl -fsS -H "Authorization: Bearer $KOSA8_API_KEY" "https://api.kosa8.com/v1/sandboxes?label=created-by=omnigent"`.
- The host logs to `/tmp/omnigent-host.log` inside the sandbox.
- `HTTP 402` on create means the org's plan has no room for another
  sandbox (or another persistent one); the message names the limit.
