# Agent compute on the cluster — the `omnigent host` stopgap

Until Omnigent has a native Kubernetes sandbox launcher
(**`omnigent-ai/omnigent#39`**), this runs the **`omnigent host` daemon as a
Deployment on the cluster** so a cluster node becomes agent compute. The daemon
dials the server's `/v1/runner/tunnel` (**outbound only** — no inbound port) and
spawns Claude Code / Codex runners locally on that node.

This is a pragmatic workaround, not the end state. The proper fix is a
server-side launcher that spawns runner Pods on demand (no per-host seeded
token). `host.yaml` is offered as a working reference for that design.

## How `host.yaml` works

It runs the **official prebaked host image**,
`ghcr.io/omnigent-ai/omnigent-host:latest` — the same image the Modal/Daytona
managed-sandbox providers boot from, published by CI from the `host` target of
[`../../docker/Dockerfile`](../../docker/Dockerfile). The image already bakes
everything a host needs:

- the full `omnigent` install (+ `git` and `tmux`), and
- the coding-harness CLIs — `claude`, `codex`, `pi` — with the Node runtime,

so there is **no runtime bootstrap** and no in-pod toolchain install: the pod
seeds its tokens from the mounted Secret and execs `omnigent host` directly.

### Two things the image already handles for you

- **`tmux` is present.** Omnigent's terminal / "new shell" feature
  (`omnigent/inner/terminal.py`) hard-fails `RuntimeError: tmux is not installed
  or not on PATH` without it; the image bakes it in.
- **`IS_SANDBOX=1` is baked in.** Claude Code refuses
  `--dangerously-skip-permissions` as root unless `IS_SANDBOX` is set, and the
  flag makes the `claude` / `codex` CLIs run without wrapping *themselves* in
  `bwrap`. So **`claude-sdk` and `codex` agents need no `bubblewrap` in the
  pod**. (This does *not* extend to terminal/`native-ui` agents — see Caveats.)

## Activate

The host authenticates to the **server** with your `omnigent login` session JWT,
and to the **harnesses** with your Claude/Codex subscription tokens (or API
keys). Mint them on a machine where you're logged into the web UI, then put them
in the `omnigent-host` Secret (see `secret.example.yaml`):

```bash
omnigent login https://omnigent.example.com   # browser -> ~/.omnigent/auth_tokens.json
claude setup-token                             # long-lived CLAUDE_CODE_OAUTH_TOKEN
# codex: a Codex access token -> CODEX_ACCESS_TOKEN (ChatGPT Business/Enterprise)
```

Copy `~/.omnigent/auth_tokens.json` into the Secret **verbatim** with
`--from-file` — it carries the token's `user_id` and `expires_at`, and a
hand-written entry that omits `expires_at` is treated as already-expired (the
host then never authenticates). See `secret.example.yaml` for the gotcha.

Because the host's session token must outlive the default 8h, raise the
session TTL on the server (`OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS`, or
`OMNIGENT_OIDC_SESSION_TTL_HOURS` if you opted into OIDC) — e.g. 720 = 30d.

## Security

This pod **runs untrusted, LLM-driven agent code with your credentials mounted**
— the `omnigent login` session JWT, your Claude/Codex tokens, and any `git`/`gh`
token you add. An agent (or a prompt-injection in its context) executes shell
commands and reads/writes the workspace inside this pod. Treat it as a trust
boundary and contain the blast radius:

- **Scope the tokens.** Use a dedicated, least-privilege GitHub token; prefer a
  short-lived/rotatable harness credential over a long-lived API key.
- **Isolate the workload.** Run it in a **dedicated namespace** on a **dedicated
  node**, separate from anything sensitive on the cluster.
- **Restrict egress.** Add a `NetworkPolicy` that permits only what the host
  needs (DNS, the Omnigent server, your model/gateway endpoints, package and git
  registries) and denies the rest of the cluster — the daemon is outbound-only,
  so nothing needs to reach *in*.

## Per-machine

One Deployment per node you want as compute — set `spec.hostname` (the host
registers under it; the CLI has no `--name` flag) and a `nodeSelector` to pin it.
Select compute with `POST /v1/hosts/{host_id}/runners`. See `host.yaml` for the
concrete pod (image, Secret mounts, optional git/gh wiring, and the workspace
volume).

## Caveats

- **amd64 only** — `omnigent==0.1.0` depends on `cel-expr-python`, which has no
  aarch64-linux wheel, so arm64 nodes can't run the host. (The prebaked host
  image is `linux/amd64` for the same reason.)
- **Terminal / `native-ui` agents don't run in an unprivileged pod.**
  `claude-sdk` and `codex` agents work; `claude-native-ui` / `codex-native-ui`
  open an interactive shell via Omnigent's terminal feature
  (`omnigent/inner/terminal.py`), whose sandbox defaults to `linux_bwrap`
  **regardless of `IS_SANDBOX`** (that flag only relaxes the harness CLI, not the
  terminal). `bwrap` isn't baked into the image, and even with it present, bwrap
  needs unprivileged user namespaces — denied in an unprivileged pod. So those
  agents fail (`omnigent/inner/bwrap_sandbox.py`:
  `linux_bwrap sandbox requires the 'bwrap' binary on PATH`). Use `claude-sdk` /
  `codex` agents, or grant the pod the needed privilege (CAP_SYS_ADMIN / a userns
  policy) or set the agent's server-side `os_env.sandbox.type=none`.
- **`git` for agents is HTTPS-only** (the image bakes no ssh client). Add a `gh`
  token to the Secret; the baked credential helper picks up
  `GIT_TOKEN`/`GIT_USERNAME` for HTTPS, and `host.yaml` rewrites `git@github.com:`
  SSH remotes to HTTPS so they're covered too. Omit it for anonymous clones of
  public repos. See `host.yaml`.
