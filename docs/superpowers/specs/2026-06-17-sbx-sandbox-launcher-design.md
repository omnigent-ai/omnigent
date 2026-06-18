# Docker `sbx` sandbox launcher — design

**Date:** 2026-06-17
**Status:** Approved (brainstorming) — ready for implementation planning

## Summary

Add a new sandbox launcher, `sbx`, that runs an Omnigent host inside a
[Docker Sandbox](https://docs.docker.com/ai/sandboxes/) (the local `sbx`
microVM CLI). The launcher mirrors the existing Daytona / Modal launchers — it
implements the provider-agnostic `SandboxLauncher` contract — but instead of a
cloud SDK it shells out to the local `sbx` binary.

The goal: start an Omnigent session inside a Docker sandbox so the session can
safely run commands in the sandbox VM, **including running other Docker
containers** for the development environment (each sbx sandbox has its own
Docker daemon). Omnigent controls that session exactly like any other
claude-native session, and the terminal is directly controllable.

## Motivation

- `sbx` gives each sandbox **its own Docker daemon, filesystem, and network** in
  a microVM. That is precisely the isolation needed to let an agent run
  arbitrary commands — and spin up dev-environment containers — without
  endangering the host.
- Omnigent already knows how to run claude-native sessions and terminals on a
  *host*. By running `omnigent host` inside the sandbox and registering it, we
  reuse all of that machinery (sessions, claude-native harness, `sys_terminal_*`)
  for free. Nothing session-specific has to be built.
- A local, disposable, Docker-native sandbox is a natural fit for users who
  already run Docker Desktop and want a YOLO-safe environment.

## Decisions (from brainstorming)

1. **Topology: local CLI bootstrap only.** `sbx` runs sandboxes locally on the
   machine where it is invoked; there is no cloud API. So this is a
   CLI-bootstrap provider (`omnigent sandbox create/connect --provider sbx`),
   run on the user's own machine. The sandbox registers with the user's Omnigent
   server (local or remote). No server-managed (`host_type="managed"`) support.
2. **Image: default sbx image + full install.** Boot sbx's default sandbox image
   (which already provides a Docker daemon) and install Omnigent into it from
   locally-built wheels, with dependencies — i.e. a *full* pip install, not the
   prebaked-host-image overlay the other launchers use.
3. **Workspace: bind-mount the current project directory.** `sbx create` mounts
   the directory the user runs the command from (read-write), so the in-sandbox
   agent and any dev containers operate on the user's real local files.
4. **Provider name: `sbx`.**
5. **Kits: supported and passed through.** Allow one or more sbx *kits* to be
   applied at provision time (e.g. the user's Claude kit at
   `https://github.com/landreville/sbxkit/tree/main/claude`).

## Approaches considered

- **A — Host-in-sandbox, mirror Daytona/Modal (chosen).** A `SandboxLauncher`
  that drives the `sbx` CLI and runs `omnigent host` inside a sandbox,
  registering it with the server. Reuses all existing session / claude-native /
  terminal machinery.
- **B — Wrap sbx's bundled `claude` agent.** Drive `sbx run claude`'s own TUI.
  Rejected: provides none of Omnigent's claude-native control or
  `sys_terminal_*` terminals — it would be a thin shell around sbx's own UI.

## Background: how the existing launcher contract works

The provider-agnostic bootstrap (`omnigent/onboarding/sandboxes/bootstrap.py`)
composes a fixed sequence of `SandboxLauncher` primitives:

- `omnigent sandbox create`:
  `prepare → provision (or attach) → keep_alive → build_wheels (local) →
  ship_wheels (put + wheel_install_command + PATH persistence) → login`.
- `omnigent sandbox connect`:
  `exec_foreground("omnigent host --server <url>")` (holds a WebSocket open
  until Ctrl-C).

`supports_local_port_forward` gates the Databricks-Apps in-sandbox OAuth login.
When it is `False` (as for Modal), the CLI sets `skip_auth=True` automatically,
so the login step is skipped entirely. The `sbx` launcher sets it `False` and
therefore inherits the same auth posture as Modal: no in-sandbox OAuth dance.

## Architecture

### New module & registration

- New file: `omnigent/onboarding/sandboxes/sbx.py` defining
  `SbxSandboxLauncher(SandboxLauncher)`.
- Register in `omnigent/onboarding/sandboxes/__init__.py` `_LAUNCHERS`:
  `"sbx": "omnigent.onboarding.sandboxes.sbx:SbxSandboxLauncher"`.
- Class vars:
  - `provider = "sbx"`
  - `supports_cli_bootstrap = True`
  - `supports_local_port_forward = False`
  - `wheel_build_index_url = None`
- **No new optional dependency.** Transport is `subprocess` against the external
  `sbx` binary, located with `shutil.which`. There is no Python SDK (contrast
  Daytona/Modal). This also means no `pyproject.toml` extra.

### Constructor & configuration

```python
SbxSandboxLauncher(
    *,
    template: str | None = None,   # -t/--template image override
    env: Sequence[str] | None = None,    # names of local env vars to forward
    kits: Sequence[str] | None = None,   # kit references to apply at create
)
```

Env-var fallbacks (resolved when the constructor arg is `None`), mirroring the
Daytona launcher's `env` + `SANDBOX_ENV_PASSTHROUGH_ENV_VAR` pattern:

- `OMNIGENT_SBX_TEMPLATE` → `template`
- `OMNIGENT_SBX_SANDBOX_ENV` (comma-separated names) → `env`
- `OMNIGENT_SBX_KITS` (comma- or whitespace-separated refs) → `kits`

### Primitive → `sbx` mapping

| Contract method | `sbx` implementation |
|---|---|
| `prepare()` | `shutil.which("sbx")` must succeed; probe login with `sbx ls` (or `sbx ls --json`). On failure raise `click.ClickException` pointing to install docs / `sbx login`. |
| `provision(name)` | `sbx create --name <name> [--kit <ref>]… [-t <template>] shell <cwd>` (bind-mounts cwd). Then run the **one-time setup step** (below). Return `name` as the sandbox id — sbx is name-addressed. |
| `attach(id)` | Confirm the sandbox exists via `sbx ls --json`; `sbx exec` auto-starts a stopped one, so no explicit start needed. |
| `keep_alive(id)` | Informational echo / no-op — local sandboxes have no cloud idle auto-stop. (Verify there is no idle timeout; if one exists, document it like Modal's 24h cap.) |
| `run(id, cmd, check)` | `sbx exec <id> bash -lc <cmd>`; capture stdout/stderr separately; echo non-empty lines; raise on non-zero when `check`. |
| `put(id, local, remote)` | `sbx cp <local> <id>:<remote>`. |
| `wheel_install_command(tgz)` | **Override** for a full install *with* deps (not `host_image_wheel_install_command`): unpack the tarball + `pip install <wheels>` (no `--no-deps` / `--force-reinstall`). |
| `exec_foreground(id, cmd)` | `sbx exec -it <id> bash -lc 'TERM=xterm-256color exec <cmd>'` with inherited stdio. Ctrl-C is delivered to the local `sbx` child, which forwards it to the remote — simpler than Modal's pidfile/`kill` workaround because the `sbx` process is a local child. Return its exit code. |
| `terminate(id)` | `sbx rm -f <id>`; idempotent — an already-removed sandbox resolves to success. |

`TERM=xterm-256color` is forced for foreground execs for the same reason as the
other launchers: native harnesses spawn tmux, which refuses to start under an
unset/dumb `TERM`.

### One-time setup step (the "full install" support)

Because we boot sbx's **default** image rather than the prebaked
`omnigent-host` image, the host's runtime dependencies are not guaranteed to be
present. After `sbx create`, `provision` runs a setup step (kept inside the
launcher so the shared `bootstrap.py` is untouched) that ensures:

- `python3` + `pip`
- `git`
- `tmux`
- the **Claude Code CLI** (claude-native needs it)

**Verification task (implementation):** enumerate exactly what sbx's default
`shell` sandbox image already provides, and install only the gap. Prefer
idempotent installs so re-provisioning is safe. The `OMNIGENT_SBX_TEMPLATE`
override lets power users point at a richer base image and skip most of this.

> Note: a user's kit (e.g. the Claude kit) handles *agent* configuration —
> `~/.claude` files, `claude plugin install …`, MCP wiring. That is orthogonal
> to this setup step, which provisions the *Omnigent host* runtime. Both run at
> create time and do not overlap.

### Kit support

- `provision` adds one `--kit <ref>` per resolved kit reference to the
  `sbx create` argv, in order.
- References pass through **verbatim** — directory, ZIP, git repo, or OCI
  artifact. sbx validates them.
- CLI: add a repeatable `--kit <ref>` option to `omnigent sandbox create`
  (in `omnigent/cli_sandbox.py`). It is generic on the command but only
  meaningful for `sbx`; threaded into the launcher via a `get_launcher`
  special-case for `provider == "sbx"`, mirroring the existing `lakebox`
  special-case that injects `workspace_host`.
- **Verification task (implementation):** confirm the git-subdirectory URL form
  `sbx` accepts for a kit living in a repo subdir (the GitHub `/tree/main/claude`
  URL vs. a `git+https://…#subdir` form vs. repo + path), using
  `sbx kit validate`, and document the working form. Passthrough is unaffected.

### Credentials

Mirror Daytona's env passthrough: `OMNIGENT_SBX_SANDBOX_ENV` (or the `env`
constructor arg) names local environment variables — e.g. `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, gateway base URLs — whose values
are forwarded into the in-sandbox host process via `sbx exec -e NAME=VALUE`.
Because this is a local flow, values are read from the invoking shell's
environment. A configured name that is unset locally fails loud (an operator
listed a credential the machine never provided), matching Daytona's behavior.

### Nested Docker & terminals (the user-facing goal)

- Each sbx sandbox has its **own Docker daemon**, so the in-session agent can
  `docker run` dev containers with no extra wiring. This is inherent to sbx, not
  something this launcher builds. (The setup step ensures a `docker` CLI is
  present to talk to that daemon, if the default image lacks it — folded into the
  verification task above.)
- Terminal control comes two ways, both free:
  - Omnigent's in-session terminals (`sys_terminal_*`, via the host's tmux).
  - Direct local attach: `sbx run <name>` or `sbx exec -it <name> bash`.

### CLI surface

No new top-level commands. The existing
`omnigent sandbox create/connect --provider sbx` works via the registry:

- `create`: provision (sbx create + kits + setup) → build wheels → ship + full
  install → (auth auto-skipped).
- `connect`: `omnigent host --server <url>` held open in the sandbox.

The only CLI change is the new repeatable `--kit` option on `sandbox create`.

## Error handling

Every primitive wraps `subprocess` failures as `click.ClickException` carrying
the `sbx` stderr verbatim plus a remediation hint — matching the SDK-boundary
error pattern in `daytona.py` / `modal.py`. Specific cases:

- `prepare`: `sbx` not on PATH → install hint; not logged in → `sbx login` hint.
- `provision`: `sbx create` failure (e.g. Docker not running, bad kit ref) →
  surface stderr.
- `terminate`: already-removed sandbox → treated as success (idempotent).
- `exec_foreground`: Ctrl-C re-raised after the remote process is torn down.

## Testing

New `tests/onboarding/sandboxes/test_sbx.py`, structured after
`tests/onboarding/sandboxes/test_daytona.py`. All `sbx` invocations are mocked
(no real sandboxes in unit tests); tests assert the exact argv per primitive:

- `prepare`: missing binary and not-logged-in both raise with actionable
  messages.
- `provision`: argv includes `--name`, each `--kit`, optional `-t`, `shell`, and
  the cwd workspace path; the one-time setup step runs.
- `run`: argv shape; non-zero exit raises when `check=True`.
- `put`: `sbx cp` source/`<id>:<dest>` argv.
- `wheel_install_command`: full-install command shape (no `--no-deps`).
- `exec_foreground`: `-it` + `TERM` + `exec`; Ctrl-C path tears down and
  re-raises.
- `terminate`: `sbx rm -f`; idempotent on already-gone sandbox.
- Kit/env resolution: constructor args win; env-var fallbacks parse correctly;
  unset passthrough name fails loud.

Implementation follows TDD (red → green → refactor) per the project's skills.

## Out of scope

- Server-managed (`host_type="managed"`) sbx hosts.
- Changes to the prebaked `omnigent-host` image (we use sbx's default image).
- Building or publishing a dedicated Omnigent sbx template image (the
  `OMNIGENT_SBX_TEMPLATE` override is the escape hatch for power users).
- The Databricks-Apps in-sandbox OAuth flow (auto-skipped via
  `supports_local_port_forward = False`).

## Open verification items (resolve during implementation)

1. Exact contents of sbx's default `shell` image → which of
   python3/pip/git/tmux/docker-CLI/Claude-Code must be installed by the setup
   step.
2. The git-subdirectory URL form `sbx --kit` / `sbx kit validate` accepts.
3. Whether local sbx sandboxes have any idle auto-stop (shapes `keep_alive`).
4. The precise `sbx ls` output / exit behavior used to detect "not logged in"
   in `prepare`.
