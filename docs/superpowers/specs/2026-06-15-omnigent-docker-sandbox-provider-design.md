# Design: `docker` sandbox provider + self-contained Compose (Sub-project A)

**Date:** 2026-06-15
**Status:** Approved for implementation planning
**Audience:** Private internal deployment (opinionated choices allowed; not an upstream contribution yet)

---

## Program context

This is **Sub-project A** of a larger effort to make Omnigent self-hostable end-to-end —
including the place agents actually execute — across Docker Compose and Kubernetes, with a
phased path to high availability. The full decomposition (agreed):

| # | Sub-project | Status |
|---|---|---|
| **A** | `docker` sandbox provider + self-contained Compose | **this spec** |
| B | `kubernetes` sandbox provider + self-contained k8s (kind/k3s first) | later |
| C | HA phase 1 — availability (1 active + standby, Postgres-backed leader election) | later |
| D | HA phase 2 — scale-out (N active; runner/host→replica directory + cross-replica forwarding) | later |

Decided constraints that carry across all sub-projects:

- **Core bet:** reuse the existing *host-in-sandbox* managed flow. New backends implement only
  the `SandboxLauncher` managed subset; the server's launch machinery is unchanged.
- **Isolation posture:** the container/pod is the isolation boundary (one managed session = one
  container/pod). Platform primitives (Docker network / k8s NetworkPolicy) own network egress
  control, not the in-process MITM egress proxy.
- **Coordination backend:** Postgres now, Redis later, behind an interface (relevant to C/D).
- **Validation:** local-first (laptop Docker for A; kind/k3s for B), then real infra.

### Explicit boundary: A does not generalize to Kubernetes

The Docker provider is a **Compose / local-Docker backend only**. It must **not** be carried
into Kubernetes by mounting a node Docker socket into the server pod. Kubernetes gets its own
launcher in Sub-project B that creates Pods/Jobs per managed session and uses
NetworkPolicy/egress-gateway primitives. The shared seam between A and B is the
`SandboxLauncher` interface, nothing more.

---

## Goal

`docker compose up` yields a stack where the server provisions a fresh, isolated
`omnigent-host` **container per managed session** — no external SaaS sandbox provider, no
bring-your-own laptop host. A user creates a `host_type="managed"` session against the local
server and an agent runs inside a sibling container that the server launched.

## Non-goals (YAGNI for A)

- CLI bootstrap path (`omnigent sandbox create --provider docker`): no `put`,
  `stream_exec`, `exec_foreground`, `wheel_install_command`, `forward_local_port`,
  local-wheel overlay.
- Warm/pre-provisioned sandbox pool.
- A hardened Docker-socket proxy (documented as a follow-up).
- Any Kubernetes work (Sub-project B).
- Multi-user auth on the self-contained profile (see Auth below — defaults to single-user).

---

## Background facts this design depends on (verified)

- **Managed launch contract.** For `host_type="managed"`, the server calls only
  `prepare()` → `provision(name)` → `run(sandbox_id, cmd)` ×N → `terminate(sandbox_id)` on
  failure/teardown (`omnigent/server/managed_hosts.py`). `_start_host_in_sandbox`
  (`managed_hosts.py:1277`) resolves `$HOME`, `mkdir`s a workspace, optionally clones a repo,
  then launches the host detached: `setsid nohup omnigent host --server <url> > log 2>&1 &`.
  No launch-flow change is needed — A only adds a launcher + config wiring.
- **`provision(name)` receives a host *display name*, not a session id**
  (`managed_hosts.py`). Labels must not assume it is a session id.
- **Provider registration seam.** `parse_sandbox_config` (`managed_hosts.py:558`) maps a
  provider name → `launcher_factory`, gated by `SUPPORTED_SANDBOX_PROVIDERS` (~line 123) and
  `PROVIDERS_WITH_MANAGED_LAUNCH` (~line 126). Reference launcher: `CWSandboxLauncher`
  (`omnigent/onboarding/sandboxes/cwsandbox.py`, smallest at 313 LOC).
- **Inner sandbox defaults are NOT `none`.** `resolve_sandbox` (`omnigent/inner/sandbox.py:321`):
  when a spec omits `os_env.sandbox.type`, Linux defaults to `linux_bwrap`
  (`_default_sandbox_for_platform`, `sandbox.py:746`). Explicit `type: none` disables the
  os_env sandbox **and rejects any read/write/network restriction** (`sandbox.py:323-329`) —
  so a `none` spec cannot also restrict reads/writes/network.
- **Native harness terminals independently require `bwrap` on Linux.** claude-native /
  codex-native / pi wrap *every* agent terminal in a bwrap OS-sandbox, mandatory and
  fail-loud (`Dockerfile:178-182`, `omnigent/inner/bwrap_sandbox.py`). This is **separate**
  from `os_env.sandbox.type`. Consequence: choosing `none` for os_env does **not** remove the
  bwrap dependency for native harnesses — bwrap-in-container must still work for those.
- **Host image ships `bwrap`.** `Dockerfile:174-176` installs `bubblewrap` in the `host`
  target.
- **Server image installs `-e .` with no extras.** `Dockerfile:127` (shared `builder` stage).
  Adding a `docker` extra to `pyproject.toml` does **not** put the Docker SDK in the server
  image unless the image build is also changed.
- **Auth gotcha for managed hosts.** A managed sandbox opens two connections back: the host
  tunnel (per-launch token, always works) and a per-session **runner tunnel** (authenticates
  with a resolved *user* credential, which the launch token lacks). Under default `accounts`
  auth the runner tunnel is **refused**
  (`deploy/kubernetes/overlays/managed-sandbox/README.md`). Working modes: single-user
  `OMNIGENT_AUTH_ENABLED=0`, or header/OIDC-proxy multi-user. `OMNIGENT_AUTH_PROVIDER`, when
  set, **overrides** the `AUTH_ENABLED`-based resolution (it is an explicit escape hatch — see
  the comments in `deploy/docker/docker-compose.yaml`), so a single-user profile must leave it
  unset/empty.
- **Provider is server-config-driven, not per-session.** `SessionCreateRequest`
  (`omnigent/server/schemas.py:1141`) carries `host_type: Literal["external","managed"]` and
  has **no** `provider` field. The route resolves the provider from
  `request.app.state.sandbox_config` (`omnigent/server/routes/sessions.py:11691`), which is
  built from the server's `sandbox:` config at startup. A client selects "use the Docker
  provider" by talking to a server **configured** with `sandbox.provider: docker`, not by a
  request field.
- **Runtime server image runs as a non-root user.** `USER 10001:10001`
  (`deploy/docker/Dockerfile:287`). A bind-mounted `/var/run/docker.sock` (typically
  `root:docker`, mode `0660`) is therefore **not** readable by the server process without
  extra Compose wiring (see A4).

---

## Design

### A1. `DockerSandboxLauncher`

New file `omnigent/onboarding/sandboxes/docker.py`, modeled on `cwsandbox.py`. Uses the
**Docker Python SDK** (`docker>=7,<8`), lazily imported, optional dependency. Rationale for
SDK over shelling out: matches the existing provider pattern, trivially mockable in unit
tests, no shell-quoting hazards, and `prepare()` can cleanly probe `client.ping()`.

Implements the **managed subset only**:

| Method | Behaviour |
|---|---|
| `prepare()` | Import the SDK (install hint on `ImportError`); `client.ping()` the daemon; raise `click.ClickException` if unreachable. |
| `provision(name)` | `client.containers.run(image, ["sleep","infinity"], detach=True, ...)` with: the configured network, injected env-passthrough secrets, resource + security options (A5), and labels (A4). Returns `container.id`. |
| `run(id, cmd, check=True)` | `container.exec_run(["bash","-lc",cmd], demux=True)` → decode stdout/stderr separately with `errors="replace"` → `RemoteCommandResult(returncode, stdout, stderr)`. Raise on non-zero when `check`. |
| `terminate(id)` | `container.remove(force=True)`; treat `NotFound` as success (idempotent). |

Error handling: convert Docker `APIError` / `NotFound` / connection errors into
`click.ClickException` with remediation hints (matches the launcher contract — callers surface
clean errors). All other managed-subset-irrelevant primitives keep the base class's
capability-error defaults.

Resolve the container handle by id per call (cache like cwsandbox's `_sandboxes` dict is
optional, since the launcher instance is per-launch via the factory).

**CLI registry:** for A, the Docker provider is wired into the *managed* path only and is **not**
added to the CLI launcher registry (no `omnigent sandbox create --provider docker`). If it is
registered there at all (e.g. so `--provider` choices list it), it must set
`supports_cli_bootstrap = False` so the CLI fails fast with a pointer to `host_type="managed"`
instead of a mid-flow capability error.

### A2. Inner-sandbox contract (load-bearing — resolved by spike)

Two facts force an explicit decision rather than an implicit default:

1. Omitting `os_env.sandbox.type` defaults to `linux_bwrap` on Linux, which needs `bwrap` to
   construct mount/PID/user namespaces — problematic inside an unprivileged container.
2. Native harnesses (claude-native/codex-native/pi) require `bwrap` **regardless** of the
   os_env type.

Therefore A2 is settled by a **spike that tests both paths** in an unprivileged Docker
container off the stock host image:

- **Path 1 — os_env `none`:** run an agent whose spec sets `os_env.sandbox.type: none`
  (container is the boundary). Confirms the SDK/non-native execution path works with no bwrap.
- **Path 2 — default `linux_bwrap`:** run with the default backend and a native harness, to
  determine whether `bwrap` works inside an unprivileged container and, if so, the *minimal*
  Docker security options required (see A5). The spike must exercise the **full bwrap
  activation path** — actual namespace creation and seccomp/filter load while wrapping a real
  agent terminal — not merely `which bwrap`. A binary that exists but cannot create namespaces
  or load its seccomp filter under the container's security profile is a failure, and the spike
  must surface that, not pass.

Decision criteria:

- **If bwrap works without extra privileges** → keep the platform defaults (no forced `none`);
  bwrap stays as defense-in-depth and native harnesses work out of the box. This is the
  preferred outcome.
- **If bwrap needs extra privileges** → record the minimal option set in A5, and document that
  native harnesses on the Docker provider require those options. For non-native agents, the
  supported posture is an explicit `os_env.sandbox.type: none` spec.

The spec ships whichever branch the spike proves; both are acceptable and the choice is
documented in `deploy/docker/README.md`. **No global, silent disabling of bwrap.** If a
provider-level defaulting path is introduced (e.g. defaulting Docker-managed sessions to
`none`), it must be deliberate and documented, not implied.

**A2 outcome (recorded 2026-06-16).** No private registry is needed — the `omnigent-host` image
builds locally from the Dockerfile's `host` target
(`docker build -t omnigent-host:local --target host -f deploy/docker/Dockerfile .`). The full
spike (`tests/onboarding/sandboxes/test_docker_bwrap_spike.py`) was run against that
locally-built image, exercising omnigent's real launcher path
(`create_exec_launcher` → `run_launcher` → bwrap):

- `os_env.sandbox.type: none` → **PASS** (resolves to `backend_type="none"`, `active=False`;
  the container is the isolation boundary).
- `linux_bwrap` activation → on the build host, **`bwrap: Creating new namespace failed:
  Operation not permitted`** — the host kernel disallows unprivileged user namespaces
  (`unprivileged_userns_clone=0`). This is a **host-kernel policy, not a provider defect**: on a
  userns-enabled host (the common default) bwrap activates. The spike now **skips with guidance**
  on userns-disabled hosts and **passes** on userns-enabled ones, so it is a clean gate.

Decision applied: the launcher does **not** force `none`; the shipped `config.managed.example.yaml`
sets `security_opt: ["no-new-privileges:true"]` and **omits `cap_drop`** (conservative — bwrap
needs userns creation), with a documented note to add `cap_drop: ["ALL"]` only once the spike
confirms it on a userns-enabled host. Native harnesses require a userns-enabled host; non-native
agents may use explicit `os_env.sandbox.type: none`. See `deploy/docker/README.md` →
*Build the host image locally* and *Security posture*.

### A3. Server wiring (`managed_hosts.py`)

- Add `"docker"` to `SUPPORTED_SANDBOX_PROVIDERS` and `PROVIDERS_WITH_MANAGED_LAUNCH`.
- Add `_docker_launcher_factory(image, env, network, resources, security)` returning a
  `DockerSandboxLauncher`.
- Add a `provider == "docker"` branch in `parse_sandbox_config` reading the
  `sandbox.docker.*` block.
- Token TTL: containers have no provider lifetime cap, so set a generous TTL (optionally
  bounded by a configured max-lifetime). The TTL must remain above any container lifetime so a
  live sandbox can re-authenticate its tunnel across reconnects.

Config block (mirrors `sandbox.cwsandbox.*`):

```yaml
sandbox:
  provider: docker
  server_url: http://omnigent:8000        # service name, reachable by sibling containers
  docker:
    image: ghcr.io/omnigent-ai/omnigent-host:latest   # default DEFAULT_HOST_IMAGE; env OMNIGENT_DOCKER_HOST_IMAGE
    network: omnigent-sbx                  # the SANDBOX network (segmented from the app/DB net); stable name, NOT <project>_default
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]  # SERVER env var NAMES injected into sandboxes
    resources:                             # all optional; see A5
      mem_limit: 4g
      nano_cpus: 2000000000                # 2 CPUs
      pids_limit: 512
```

### A4. Compose integration (`deploy/docker/`)

- **Server image must carry the Docker SDK.** Add `docker = ["docker>=7,<8"]` to
  `[project.optional-dependencies]` in `pyproject.toml`, **and** install it into the server
  image. Recommended: add an explicit install line in the `server-builder` stage (mirroring
  the `psycopg[binary]` line at `Dockerfile:145`) so the **host** image stays lean. (Adding
  the extra at the shared `builder` line 127 would also bloat the host image — avoid.)
- **Mount the Docker socket into the server service only:** `/var/run/docker.sock` →
  the `omnigent` service. **Never** mount the socket into sandbox containers. Document plainly
  that socket access is host-root-equivalent (accepted for an internal deploy; socket-proxy
  hardening is a follow-up).
- **Socket must be *usable* by the non-root server (UID 10001).** A bare bind-mount is not
  enough — the socket is typically `root:docker 0660`, so `client.ping()` fails as UID 10001.
  The managed overlay MUST resolve this explicitly. MVP choice (pick one, documented):
  `group_add: ["${DOCKER_GID}"]` on the `omnigent` service with `DOCKER_GID` set to the host's
  docker-group GID (`getent group docker`); **or** run the managed profile's server as root
  with the tradeoff stated; **or** promote the socket-proxy follow-up into MVP. Recommended:
  `group_add` + a documented `DOCKER_GID` in `.env.example`. `prepare()`'s `client.ping()` is
  the fail-fast check that this wiring is correct.
- **Two networks, segmented.** Define two named networks (stable names so
  `sandbox.docker.network` is fixed, not the generated `<project>_default`): an **app/DB
  network** joining `omnigent` + `postgres`, and a **sandbox network** (e.g. `omnigent-sbx`)
  joining `omnigent` + spawned host containers. Sandbox containers join **only** the sandbox
  network — they reach the server at `http://omnigent:8000` and get outbound NAT, but have **no
  route to Postgres**. The launcher attaches provisioned containers to the sandbox network via
  `sandbox.docker.network`.
- **Networking MVP = simple path.** The **sandbox network is non-internal** so sandboxes keep
  normal outbound (LLM/Git) egress via Docker's default NAT while reaching the server by service
  name. Egress is therefore **coarse** (host-level / Docker NAT), explicitly documented as such;
  the segmentation above only removes the Postgres route, it does not finely control egress. The
  stricter path — an `internal` control network plus a dedicated egress proxy/router container
  that is the only outbound path — is a documented **follow-up**, not MVP. (Note: making the
  sandbox network `internal` alone would break LLM/Git egress, so it is not a drop-in.)
- **Auth: self-contained profile is single-user.** Because the per-session runner tunnel is
  refused under default `accounts` auth, the managed-Docker Compose profile sets
  `OMNIGENT_AUTH_ENABLED=0` (single trusted tenant, no login). Document header/OIDC-proxy as
  the multi-user upgrade path (consistent with the k8s managed-sandbox overlay). Ship this as a
  dedicated compose profile/overlay (e.g. `docker-compose.managed.yaml`) layered on the
  existing stack rather than mutating the default single-server compose. The overlay MUST also
  leave `OMNIGENT_AUTH_PROVIDER` unset/empty under `OMNIGENT_AUTH_ENABLED=0` — an explicit
  provider overrides the `AUTH_ENABLED` resolution and would silently re-enable auth (and thus
  re-break the runner tunnel).
- Pre-pull / document `docker compose pull` for the host image.

### A5. Container resource & security options

Set on `containers.run` at provision time.

**Unconditional:**
- `mem_limit` (e.g. `4g`), `nano_cpus` or CPU quota (e.g. 2 CPUs), `pids_limit` (e.g. 512).
- `init=True` (reap zombies from the detached host process tree).
- Labels (A6).

**Spike-gated (A2 Path 2 determines these):**
- `security_opt=["no-new-privileges:true"]`, `cap_drop=["ALL"]`, and any seccomp setting.
  These **conflict with bwrap's** need to create user namespaces and perform mounts. They may
  only be applied if the spike confirms native-harness bwrap (or the chosen posture) still
  works under them. The spec records the minimal viable set; it does not assume `cap_drop=ALL`.

### A6. Lifecycle, labels, and the reaper

**Labels** (set at provision; `name` is a host display name, not a session id):
- `omnigent.managed=1`
- `omnigent.provider=docker`
- `omnigent.host_name=<name>`
- `omnigent.created_at=<epoch-seconds>`

**Reaper** (in-server, not a sidecar — the server already owns `HostStore`, session state,
managed-launch lifecycle, and teardown; a sidecar would need DB coupling or a new server API
just to learn what is live; managed runner state is single-replica anyway):
- Runs on server startup and on a periodic sweep.
- Reaps containers labeled `omnigent.managed=1` + `omnigent.provider=docker` whose container
  id is **not referenced by any current Docker-provider host row** in `HostStore` — *current*
  meaning the row exists, **regardless of online/offline state**. A host that is merely offline
  (tunnel dropped, reconnecting) must be **preserved**, not reaped; only containers with no
  backing row at all (or rows past an explicitly-defined expiry, if such a policy is added
  later) are removed. Avoid conflating "live" with "online".
- **Requires a new store API.** Today `HostStore` exposes only owner-scoped `list_hosts(owner)`
  (`omnigent/stores/host_store.py:488`) and point lookup — there is no "all managed sandbox ids
  for a provider" query. Add `list_managed_sandbox_ids(provider: str) -> set[str]` to
  `HostStore` (returning the provider's recorded sandbox/container ids across all owners) and
  have the reaper diff the Docker-listed managed container ids against it.
- Applies a **grace period** (skip containers whose `omnigent.created_at` is within the last
  N seconds) so the periodic reaper cannot kill a just-provisioned container before its host
  finishes registering.

### A7. Data flow

Provider is **not** a request field — it comes from the server's `sandbox.provider: docker`
config. The client just asks for a managed host:

```
(server started with sandbox.provider: docker)
POST /sessions {host_type: managed}
  → route reads provider from app.state.sandbox_config (= docker)
  → server mints launch token + host_id + host_name
  → DockerSandboxLauncher.prepare()                 # daemon reachable
  → provision(host_name)                            # container up on omnigent-sbx, labeled
  → run($HOME) / run(mkdir workspace) / run(clone)  # via exec_run
  → run("setsid nohup omnigent host --server http://omnigent:8000 ... &")
  → host dials back: host tunnel (launch token) + runner tunnel (server credential)
  → session binds to the registered host → agent executes
DELETE /session  → terminate(container)             # docker rm -f, idempotent
crash/restart    → reaper removes orphaned managed containers (after grace period)
```

## Error handling summary

- Daemon unreachable → `prepare()` fails fast with a hint.
- Provision failure → surface `click.ClickException`; server's existing failure path calls
  `terminate`.
- `run` non-zero with `check=True` → `click.ClickException` naming the command + exit code.
- `terminate` on a missing container → success (idempotent).
- Orphaned containers after a server crash → reaped on next startup / sweep (grace-period
  guarded).

## Testing

- **Unit (mocked Docker SDK client):**
  - `provision` builds correct `containers.run` args — image, network, env-passthrough,
    labels (incl. `created_at`), and the resource/security options.
  - `run` maps a `demux=True` exec result into `RemoteCommandResult` (separate stdout/stderr,
    `errors="replace"` decode); non-zero + `check` raises.
  - `terminate` is idempotent (`NotFound` → success).
  - Docker `APIError` → `click.ClickException`.
  - Reaper: selects only unreferenced managed containers; respects the grace period; ignores
    non-managed containers.
- **Integration (opt-in marker; requires a Docker daemon):**
  - Real `provision → run → terminate` against the host image; no leaked containers.
  - Full managed-session smoke test: server started with `OMNIGENT_AUTH_ENABLED=0` (and
    `OMNIGENT_AUTH_PROVIDER` unset) **and** `sandbox.provider: docker` → `POST /sessions
    {host_type: managed}` → agent executes a trivial task → teardown leaves no containers.
  - Store API: `list_managed_sandbox_ids("docker")` returns recorded ids across owners; reaper
    preserves containers backed by an offline-but-present host row and removes only unbacked
    ones (grace period respected).
- **Spike (A2):** dedicated, documented test of both inner-sandbox paths (os_env `none`;
  default `linux_bwrap` + native harness) in an unprivileged container; records the minimal
  security-option set for A5.

## Open risks

1. **bwrap-in-unprivileged-container (A2/A5)** — the linchpin. Resolved by the spike before
   the launcher hardening is finalized.
2. **Coarse egress (A4)** — MVP relies on Docker NAT; fine-grained egress control is a
   follow-up (internal network + egress router). Acceptable for an internal trusted deploy.
3. **Docker socket = host root (A4)** — accepted for internal use; socket-proxy hardening is a
   follow-up.
4. **Socket permission portability (A4)** — the non-root server (UID 10001) needs the host
   docker-group GID via `DOCKER_GID`, which varies by host. `prepare()`'s `client.ping()` fails
   fast when it is wrong, but it is a documented setup step, not zero-config. Socket-proxy or
   run-as-root sidesteps it.

## Files touched

- `omnigent/onboarding/sandboxes/docker.py` (new)
- `omnigent/server/managed_hosts.py` (provider registration + factory + config parse)
- `omnigent/stores/host_store.py` (`list_managed_sandbox_ids(provider)` for the reaper)
- `pyproject.toml` (`docker` extra)
- `deploy/docker/Dockerfile` (install Docker SDK in the `server-builder` stage)
- `deploy/docker/docker-compose.yaml` + new managed overlay (`docker-compose.managed.yaml`),
  `.env.example`, `README.md`
- Tests under `tests/` (unit + opt-in integration + spike)
