# Agent compute on the cluster — the `omnigent host` stopgap

Until Omnigent has a native Kubernetes sandbox launcher
(**`omnigent-ai/omnigent#39`**), this runs the **`omnigent host` daemon as a
Deployment on the cluster** so a cluster node becomes agent compute. The daemon
dials the server's `/v1/runner/tunnel` (**outbound only** — no inbound port) and
spawns Claude Code / Codex runners locally on that node.

This is a hand-built workaround, not the end state. The proper fix is a
server-side launcher that spawns runner Pods on demand (no per-host seeded
token). `host.yaml` is offered as a working reference for that design.

## How `host.yaml` works

A stock `node:22` pod **runtime-bootstraps** on first start (no baked image —
deliberate for a stopgap; cached on the PVC across restarts):

1. installs an **agentic-dev toolchain** (see below) via `micromamba` +
   conda-forge into the PVC (no root — the pod runs as uid 1000);
2. installs the `omnigent` CLI (via `uv`) + the `claude` / `codex` CLIs (npm);
3. seeds the session + harness tokens from a mounted Secret;
4. runs `omnigent host --server <SERVER_URL>`.

### The toolchain matters (hard-won)

- **`tmux` is required** — Omnigent's terminal / "new shell" feature
  (`inner/terminal.py`) hard-fails `RuntimeError: tmux is not installed or not on
  PATH` without it.
- **`bubblewrap` (`bwrap`)** — the `claude-native` / `codex-native` harnesses
  wrap the CLI in a bwrap sandbox; without it they fail at
  `terminal.py` `linux_bwrap sandbox requires the 'bwrap' binary on PATH`.
  Rootless bwrap works in-pod via Omnigent's `--unshare-user-try` profile.
- **ffmpeg + language servers** (ts/bash/yaml/json/pyright/gopls/rust-analyzer +
  ripgrep/fd/jq) — so agents have a real dev environment.

A baked CI image would remove the runtime install; it's left runtime for the
stopgap.

## Activate

The host authenticates to the server with **your `omnigent login` session JWT**,
and to the harnesses with **your Claude/Codex subscription or API keys**. Mint
them on a machine where you're logged into the web UI, then put them in the
`omnigent-host` Secret (see `secret.example.yaml`):

```bash
omnigent login https://omnigent.example.com   # browser -> ~/.omnigent/auth_tokens.json
claude setup-token                             # long-lived CLAUDE_CODE_OAUTH_TOKEN
# codex: read the access token from ~/.codex/auth.json
```

Because the host's session token must outlive the default 8h, raise
`OMNIGENT_OIDC_SESSION_TTL_HOURS` on the server (e.g. 720 = 30d).

## Per-machine

One Deployment per node you want as compute — set `spec.hostname` (the host
registers under it; the CLI has no `--name` flag) and a `nodeSelector` to pin it.
Select compute with `POST /v1/hosts/{host_id}/runners`.

## Caveats

- **amd64 only** — `omnigent==0.1.0` depends on `cel-expr-python`, which has no
  aarch64-linux wheel, so arm64 nodes can't run the host.
- **`git`/`gh` for agents** is optional and graceful: mount an SSH key +
  `gh` token in the Secret and the bootstrap wires them (incl. routing
  `git@github.com:` remotes through HTTPS so a separate GitHub SSH key isn't
  needed). Omit them and the bootstrap simply skips that step.
