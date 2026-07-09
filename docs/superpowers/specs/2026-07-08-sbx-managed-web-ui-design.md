# Server-managed `sbx` sandboxes from the Web UI — design

**Date:** 2026-07-08
**Status:** Approved (brainstorming) — ready for implementation planning

## Summary

Let a user create a new `sbx` (Docker Sandbox microVM) instance directly from
the Omnigent Web UI's new-session flow, alongside picking an already-connected
sbx host. We do this by making `sbx` a **managed-launch provider** — reusing
the existing `host_type="managed"` pipeline that Modal / Daytona / Islo already
use. The server (co-located with the `sbx` daemon) provisions a fresh sbx
microVM per session from a **prebaked omnigent-host image**, runs `omnigent
host` inside it, binds it to the session, and tears it down with the session.

Nothing new is needed in the session/host-picker UX: connected sbx hosts
already appear as pickable rows, and the picker's existing "New Sandbox" option
becomes "Sbx Sandbox" once the server advertises sbx as its managed provider.

## Motivation

- Today `sbx` is **CLI-bootstrap only**: `omnigent sandbox create --provider
  sbx` provisions locally, then `omnigent sandbox connect` registers it as an
  external host. There is no way to spin up a new sbx instance from the Web UI.
- The `host_type="managed"` flow already does exactly the server-side
  provision → run `omnigent host` → bind → teardown lifecycle we need. Adding
  sbx to it is mostly wiring, because the managed flow's default `start_host()`
  drives everything through `run()`, which the sbx launcher already implements.
- sbx has a property the cloud providers lack: it **retains its filesystem
  across idle-stop and auto-starts on the next `exec`**. That makes a dormant
  managed sbx host cheaply *resumable in place* (workspace intact) — a real
  advantage over Modal's "dormant host = start a new session".

## Decisions (from brainstorming)

1. **Topology: server host runs `sbx create`.** The managed flow runs on the
   Omnigent server process, which must have the `sbx` binary + Docker + KVM and
   be signed in (`sbx login`). This is the local-dev / self-hosted single-box
   deployment. (Not the user's desktop; not a remote-server-with-local-sbx
   split.)
2. **Lifecycle: ephemeral per-session.** One fresh sbx sandbox per session,
   bound to it, torn down / relaunched with it — identical to Modal/Daytona.
   The new instance is **not** a reusable pool host.
3. **Omnigent delivery: prebaked template built on sbx's docker base.** Managed
   launch does not ship wheels; the sbx sandbox boots from an image that already
   has omnigent-host installed (`sandbox.sbx.template`). The image is built
   **`FROM docker/sandbox-templates:shell-docker`** (sbx's own nested-Docker
   base) with omnigent layered on top — NOT the vanilla `omnigent-host:latest`
   image, which is `python:slim` with no Docker daemon and would lose sbx's
   nested-Docker capability (the whole reason to pick sbx). Building that image
   is **in scope** for this spec.
4. **Resume: in scope.** `can_resume = True` + a `resume()` implementation, so a
   dormant managed sbx host wakes in place with its workspace.

## Approaches considered

- **A — sbx as a managed-launch provider (chosen).** Wire sbx into
  `managed_hosts.py` exactly like Daytona, and teach `SbxSandboxLauncher` a
  managed mode. Reuses the whole managed pipeline, the Web UI "New Sandbox"
  option, host binding, relaunch, and teardown. Minimal new surface.
- **B — new "provision persistent host from UI" path.** A bespoke endpoint +
  UI that provisions an sbx instance and registers it as a durable, reusable
  host. Rejected: the user chose ephemeral-per-session, and this would
  duplicate machinery the managed flow already provides.
- **C — desktop/Electron shells out to local `sbx`.** The desktop app runs
  `sbx` on the user's own machine and registers the result. Rejected: the user
  chose the server host as the execution location.

## Background: the managed-host contract (what we plug into)

`omnigent/server/managed_hosts.py` drives a managed launch as:

```
launcher.prepare()
sandbox_id = launcher.provision(host_name)        # server picks host_id/name
launcher.start_host(sandbox_id, token=…, host_id=…, host_name=…,
                    server_url=…, repo_url=…, repo_branch=…, repo_name=…)
_wait_for_host_online(...)                          # poll hosts table
# teardown / relaunch / wake as sessions come and go
launcher.terminate(sandbox_id)
launcher.resume(sandbox_id)                         # only if can_resume
```

Key facts that make sbx a near-free fit:

- **`start_host()` has a default "exec model" implementation** (base.py): it
  probes `$HOME`, makes `$HOME/workspace`, optionally `git clone`s the repo,
  then `run_background`s `OMNIGENT_HOST_TOKEN=… OMNIGENT_HOST_ID=… OMNIGENT_HOST_NAME=…
  omnigent host --server <url>`. Everything runs through `run()` / `run_background()`.
  sbx already implements `run()`, so sbx inherits `start_host()` **if** its
  execs carry the harness credentials and can reach the server.
- **The launch token/identity ride the command as shell `VAR=val` prefixes**,
  not `sbx exec -e` — so no special handling is needed for those three.
- **`/v1/info` exposes `managed_sandboxes_enabled` + `sandbox_provider`**, which
  gate and label the Web UI's sandbox option. Setting `provider: sbx` lights up
  a "Sbx Sandbox" row automatically.
- **Config is one provider per server.** `sandbox.provider` is a single value —
  enabling managed-sbx means the server's managed option *is* sbx (not also
  Modal). Acceptable for the target deployment.

## Architecture

### 1. Server wiring — `omnigent/server/managed_hosts.py`

Mirror the Daytona path:

- Add `"sbx"` to `SUPPORTED_SANDBOX_PROVIDERS` and to
  `PROVIDERS_WITH_MANAGED_LAUNCH`.
- Add `SBX_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600` (sbx has no platform lifetime
  cap; the bound is policy, mirroring Daytona — long enough for a live/resumed
  sandbox to re-authenticate its tunnel, short enough to expire tokens of
  sandboxes nobody deleted).
- Add `_sbx_launcher_factory(*, template, env, kits)` returning a factory that
  builds `SbxSandboxLauncher(template=…, env=…, kits=…, server_url=…)` (lazy
  import). The factory bakes in `config.server_url` so managed provision can
  open egress to it (see §2.5).
- Add an `elif provider == "sbx":` branch in `parse_sandbox_config` parsing the
  `sandbox.sbx` block:
  - `template` (**required for managed** — the default sbx image has no
    omnigent; fail loud at parse time if absent so an operator typo stops
    startup, not the first launch). Maps to the launcher's create image
    (`sbx create -t <template>`).
  - `env` (optional) — SERVER-process env var **names** (harness LLM
    credentials, gateway URLs, `GIT_TOKEN`) injected into the host exec.
  - `kits` (optional) — sbx kit references applied at provision.

Example server config:

```yaml
sandbox:
  provider: sbx
  server_url: http://host.docker.internal:6767   # what the in-box host dials back to
  sbx:
    template: ghcr.io/you/omnigent-host-sbx:latest
    env: [ANTHROPIC_API_KEY, OPENAI_API_KEY, GIT_TOKEN]
    kits: [/opt/sbxkits/claude]
```

### 2. Launcher — `omnigent/onboarding/sandboxes/sbx.py`

`SbxSandboxLauncher` gains a **managed mode** without losing its CLI-bootstrap
mode. The constructor accepts the new `server_url` (and already accepts
`template` / `env` / `kits`). A launcher built with `server_url` set operates in
managed mode.

**2.1 Managed `provision(name)`.** Diverges from the CLI-bootstrap provision:

- Create from the template, **no cwd bind-mount** — the server has no per-user
  cwd, and `start_host()` creates `$HOME/workspace` inside the box and clones
  there. (Discovery: confirm `sbx create ... shell` accepts *no* host PATH; if a
  PATH is mandatory, bind-mount a throwaway empty server-side dir.)
- **Skip the omnigent-install setup step** — the template already carries
  omnigent + tmux + the harness CLIs. Keep it idempotent so a lean template
  still works.
- Keep the **default-network-policy preflight** and the **Claude-auth-domain
  allow** already in the CLI provision.

**2.2 Rely on the default `start_host()`.** No override needed once §2.5 and
§2.6 hold: the default probes `$HOME`, makes the workspace, clones the repo, and
backgrounds the host — all via `run()` / `run_background()`.

**2.3 `run_background` survival (discovery).** The default `run_background`
wraps the host in `setsid nohup … &`. Confirm the backgrounded process
**survives `sbx exec` returning** (like Modal/Daytona). If sbx reaps children
when the exec session ends (like OpenShell), override `run_background` to hold
the exec stream open instead.

**2.4 `terminate` / `prepare`.** Already implemented. `prepare` (sbx installed +
logged in + default network policy) now also gates the server at launch time;
its ClickExceptions surface as the managed-launch 502.

**2.5 Egress to the server + AI endpoints.** The sbx sandbox is default-deny, so
the in-box `omnigent host` cannot dial back to `server_url` without an allow
rule. Managed provision (or the start path) must
`sbx policy allow network --sandbox <name> "<server-host:port>,<AI/registry
domains>"`, extending the existing Claude-auth-domain allow. The launcher knows
`server_url` from its constructor. (Discovery: confirm the server-URL allow is
sufficient for dial-back from inside the box, per the original sbx design's
`host.docker.internal` finding.)

**2.6 Credential injection via `sbx exec -e`.** Unlike Daytona (env rides
sandbox creation), sbx has no persistent per-sandbox env — env is per-`exec`.
So the launcher must inject the configured `env` names, **resolved from the
server process environment**, as `-e NAME=VALUE` on the execs that run the host.
Implementation: a single exec-arg builder used by `run` / `run_background` (and
the existing `exec_foreground`), so the inherited `start_host` needs no change.
A configured name unset in the server env fails loud (mirrors Daytona). The
`proxy-managed` placeholder-stripping the CLI mode already does applies here too.

**2.7 Resume in place (`can_resume = True`).** sbx retains the sandbox
filesystem across idle-stop and auto-starts on the next `exec`, so a dormant
managed sbx host's workspace survives. Implement `resume(sandbox_id)` to bring
the compute + filesystem back (verify the sandbox still exists; `sbx start`
it — or rely on `exec` auto-start). The server's wake path
(`resume_managed_host`) then re-arms a fresh token and re-runs `start_host`
against the SAME sandbox id, with `repo_url=None` (the workspace already holds
the clone). A failed wake must not tear the sandbox down.

### 3. Web UI — `web/src/lib/capabilities.ts`

Essentially free. Once the server reports `managed_sandboxes_enabled: true` +
`sandbox_provider: "sbx"`:

- The new-session host picker shows a "Sbx Sandbox" option (via the existing
  `managed_sandboxes_enabled` gate + `sandboxOptionLabel`).
- Picking it creates a `host_type="managed"` session (no `host_id` / workspace,
  or a repo-URL workspace from the existing sandbox repo inputs).
- Existing connected sbx hosts already render as pickable rows.

**Only change:** add `sbx` to `_SANDBOX_PROVIDER_NAMES` so the label reads
nicely (e.g. `sbx → "Docker"` → "Docker Sandbox", or `"Sbx"`). Without it the
fallback title-cases the id to "Sbx Sandbox", which is acceptable — this is
polish.

### 4. Prebaked omnigent-host sbx image (in scope)

The heaviest deliverable. **Base: `FROM docker/sandbox-templates:shell-docker`**
— sbx's own nested-Docker template — with omnigent layered on top. This
guarantees the Docker daemon is present (it ships in the base) instead of hoping
a `python:slim` image carries it. Explicitly NOT the vanilla `omnigent-host`
image: that stage is `python:3.x-slim` with no `dockerd`, so it would run
`omnigent host` but leave the sandbox with no `docker` — removing the reason to
use sbx at all. Requirements:

- **Layer the omnigent-host install** onto the base: the venv + full omnigent
  install, git / tmux, and the coding-harness CLIs (Claude Code, Codex, pi) —
  reusing the `--target host` stage's install steps
  (`deploy/docker/Dockerfile`) as the recipe, adapted to the `shell-docker`
  base's OS (Ubuntu, per the sbx spike) rather than Debian slim. Keeps managed
  launch install-free so a host registers in seconds.
- **Preserve the base's Docker daemon + sbx plumbing** — do not strip or
  override the base's entrypoint/daemon setup. Verify `docker run` still works
  after the omnigent layer (discovery item #4).
- Published to a registry the server can `sbx create -t` from, referenced by
  `sandbox.sbx.template`.
- Lives under `deploy/sbx/` with a Dockerfile + README (mirroring
  `deploy/modal/`, `deploy/daytona/`), including how to build/publish and the
  required `sbx policy` / `sbx login` server prerequisites.
- **Verify `sbx create -t <registry-ref>` accepts an arbitrary OCI image** at
  all (the original spike only exercised sbx's default template) — a quick
  live probe before the build is finalized.

## Data flow (create new sbx instance)

1. User opens the new-session composer, picks **Sbx Sandbox** (optionally a repo
   URL + branch), sends the first message.
2. `POST /v1/sessions {host_type: "managed", workspace?: "<repo-url>[#branch]"}`
   returns immediately; a background task runs `launch_managed_host`.
3. `prepare` (sbx present/logged-in/policy) → `provision` (create from template,
   allow egress) → pre-register host row with the launch-token digest →
   `start_host` (mkdir workspace, clone repo, background `omnigent host` with
   token + harness creds) → poll until the host is online.
4. Session binds to the new host; the runner launches; the turn proceeds.
5. On session delete → `terminate` (sbx rm -f) + host-row delete. On idle-stop
   then a new message → `resume` wakes it in place; on hard death → relaunch
   provisions a fresh generation and re-clones the repo.

## Error handling

Reuse the managed flow's boundaries — every launcher failure is a
`click.ClickException` surfaced as a managed-launch `502` with the sbx stderr:

- `prepare`: sbx missing / not logged in / no default network policy →
  actionable hints (already implemented for CLI mode).
- `provision`: `sbx create` failure (Docker/KVM down, bad template ref) →
  surface stderr; template pull failure names the image.
- egress allow failure → warn but continue only if non-fatal; if the host then
  can't register, the online-timeout 502 points at `/tmp/omnigent-host.log`.
- `start_host` clone failure names the repository (default impl already does).
- teardown / wake failures are best-effort / non-tearing per the managed
  contract.

## Testing

New `tests/onboarding/sandboxes/test_sbx.py` additions (mock all `sbx`
invocations), plus server-config tests:

- **Launcher managed mode**: managed `provision` argv (template `-t`, no
  bind-mount, no install step, egress allow); `run`/`run_background` inject
  `-e` for configured env resolved from the server env; a missing configured
  env name fails loud; inherited `start_host` produces the expected clone +
  backgrounded-host commands; `resume` argv; `can_resume is True`.
- **Config parsing** (`test_managed_hosts` / config tests): `sandbox.provider:
  sbx` builds a launcher factory; missing `sandbox.sbx.template` fails loud;
  `env` / `kits` parse; `sbx` is in both provider sets; token TTL is the sbx
  constant.
- **Capabilities**: `/v1/info` advertises `managed_sandboxes_enabled: true` +
  `sandbox_provider: "sbx"` when configured; `sandboxOptionLabel("sbx")` →
  expected label (web unit test).

Follow TDD (red → green → refactor).

## Out of scope

- Persistent / reusable-pool sbx hosts created from the UI (this flow is
  ephemeral per-session).
- Desktop/Electron-driven local sbx creation.
- Multiple simultaneous managed providers per server (one `sandbox.provider`).
- Changing the CLI-bootstrap sbx flow (`omnigent sandbox create/connect`), which
  continues to work unchanged and remains the way to register a *reusable* sbx
  host.

## Discovery items to resolve during implementation

1. Does `sbx create … shell` accept **no** host PATH (for the no-bind-mount
   managed provision)? If not, bind-mount a throwaway empty server dir.
2. Does a `setsid nohup` background **survive `sbx exec` returning**, or is it
   reaped (needing a stream-holding `run_background` override)?
3. Is a per-sandbox `sbx policy allow network` for the server `host:port`
   sufficient for the in-box host to dial back (revisit the
   `host.docker.internal` egress finding from the original sbx design)?
4. Does an omnigent-host layer built on `shell-docker` **retain the nested
   Docker daemon** (`docker run --rm hello-world` works after the layer), and
   does `sbx create -t <arbitrary-registry-ref>` accept the image at all?
5. Exact `resume` mechanics: `sbx start <id>` vs. relying on `exec` auto-start
   before `start_host`.
