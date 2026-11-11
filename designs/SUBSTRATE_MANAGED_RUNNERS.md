# Substrate managed runners on Kubernetes

**Status:** Implementation in progress. The experimental provider, activation
protocol, VOLUMES lifecycle primitives, and deployment overlay are implemented.
Automatic idle suspension and distributed transition fencing remain incomplete.
The core gVisor lifecycle has passed a real-cluster smoke; broader production
and harness qualification remains incomplete. See the
[experimental runbook](../deploy/kubernetes/overlays/substrate-runners/README.md).

## Goal and recommendation

Add `substrate` as an Omnigent managed-sandbox provider. Keep the Omnigent server,
its database, and ingress on ordinary Kubernetes infrastructure. Run each managed
host and its session runners inside a Substrate Actor.

This is the managed-runner deployment pattern, not a proposal to run the Omnigent
server as an Actor. Users should select Substrate through the existing managed
session workflow, send messages, retain their workspace while idle, and resume
without selecting a new host.

Recommended first supported release:

- A built-in `SandboxHostLauncher` adapter with optional client dependencies.
- Operator-managed Substrate installation, WorkerPools, and ActorTemplates.
- One Actor per managed host, not per message, tool invocation, or Kubernetes Pod.
  Sessions sharing a host share its Actor and lifecycle.
- Durable HOME/workspace snapshots using `SNAPSHOT_FIDELITY_VOLUMES`, with
  golden snapshots disabled. Inspect the restored bootstrap before deciding
  whether the host survived locally or needs a fresh activation.
- Explicit server-driven wake and safe idle suspension. Do not rely on traffic
  idleness or keep an Actor warm just because its host tunnel is connected.
- Existing Omnigent launch-token authentication and owner-bound credential
  brokers. Document the credential limitations below rather than promising a
  credential-free sandbox.

Memory-preserving resume and credential-free actor authentication are follow-ups.
They should not delay a useful deployment target, but they must not be implied
by the initial support claim.

## Evidence and existing seams

Inspected Omnigent at `ea4cb3fbd3` and Substrate at `94e0a26a`. Substrate references
below are pinned to that checkout; select and verify a released version in phase
0 before publishing installation instructions.

| Existing implementation | Reuse or constraint |
| --- | --- |
| [Sandbox interfaces](../omnigent/onboarding/sandboxes/base.py) | Implement `SandboxHostLauncher`; no new runner protocol or general-purpose exec interface is needed. |
| [Provider registry](../omnigent/onboarding/sandboxes/registry.py) and [managed hosts](../omnigent/server/managed_hosts.py) | Reuse provider selection, owner binding, launch progress, host registration, wake, teardown, and multi-provider configuration. |
| [Kubernetes launcher](../omnigent/onboarding/sandboxes/kubernetes.py) | Reuse workspace preparation, clone policy, managed config writing, and host supervision. Do not inherit its Job/Secret/Pod lifecycle. |
| [Warm bootstrap](../omnigent/host/warm_bootstrap.py) | Reuse assignment validation and preparation semantics where possible. Its Pod UID, exec transport, and single-lifetime binding cannot be copied unchanged. |
| [Managed keepalive](../omnigent/server/managed_host_keepalive.py) | Already receives runner work/idle signals. It renews providers but does not implement a general idle-stop controller. |
| [Managed sandbox reaper](../omnigent/server/managed_sandbox_reaper.py) | Reuse durable teardown/retry behavior; retention deletion is distinct from idle suspension. |
| [Lambda launcher](../omnigent/onboarding/sandboxes/lambda_microvm.py) | Existing `resume_preserves_host` capability avoids starting a second host after a memory thaw. It is not sufficient for every Substrate restore. |
| [Substrate public schema](https://github.com/agent-substrate/substrate/blob/94e0a26a/api/ateapi/v1/ateapi.proto) | Actor lifecycle is the Control gRPC interface. WorkerPools are Kubernetes resources; Actors and ActorTemplates are not ordinary Pod manifests. |
| [Substrate interface guide](https://github.com/agent-substrate/substrate/blob/94e0a26a/docs/reference/api-guide.md) | Actor creation has no per-actor command/env override. Templates are immutable. `durableDir` participates in snapshots. Memory restore may fall back to ROOTFS cold boot. |
| [Egress contract](https://github.com/agent-substrate/substrate/blob/94e0a26a/docs/guides/egress-traffic.md) | Inspected HTTP(S) blocks WebSocket upgrades. TLS passthrough permits WSS but cannot inject HTTP credentials. |

## Deployment architecture

```text
User / API client
        |
Omnigent ingress -> Omnigent server Deployment -> Omnigent database
                          |
                          +-- Control gRPC -> ate-api-server
                          |                  create / inspect / suspend / resume / delete
                          |
                          +-- authenticated atenet-router -> Actor bootstrap
                                                             |
Substrate WorkerPool -> Actor sandbox -> omnigent host -> session runner(s)
                                            |
                                            +-- outbound WSS via TLS passthrough
                                                back to Omnigent
```

Substrate owns placement, sandbox isolation, worker capacity, snapshots, and
storage cleanup. Omnigent owns host/session identity, Actor assignment, activation,
work-aware suspension, and session-facing errors. Kubernetes owns the server
Deployment and Substrate infrastructure.

Do not create a Kubernetes Job for each Actor, run `kubectl` in the server request
path, or implement another scheduler/warm pool alongside Substrate.

### Operator-owned resources

- An existing supported Substrate installation with snapshot storage configured.
- A dedicated atespace for this Omnigent deployment and versioned ActorTemplates.
  An atespace is not a Kubernetes namespace.
- WorkerPools with explicit runtime class, CPU architecture, capacity, and labels
  selected by the templates. Validate gVisor first; qualify microVM separately.
- A template using a digest-pinned Omnigent host image, a non-root workload,
  explicit actor CPU/memory limits, durable HOME, and a bootstrap entrypoint.
- Substrate Control access for a dedicated Omnigent ServiceAccount, plus separate
  router client identity and ingress allowlisting for activation.
- Network access from the server namespace to the Control endpoint and router,
  and approved actor egress destinations.

Omnigent creates Actors and their egress policies. It reads, but never silently
creates, resizes, or edits operator templates or pools during a session launch.
The current atespace-editor role also permits template changes: treating templates
as operator-owned is an application convention, not a finer-grained upstream
permission guarantee.

### Proposed configuration

The experimental provider accepts the following configuration:

```yaml
sandbox:
  provider: substrate
  server_url: https://omnigent.example.com
  substrate:
    endpoint: api.ate-system.svc:443
    atespace: omnigent-runners
    actor_template: omnigent-host-v1
    api_ca_file: /var/run/substrate/api-ca.pem
    api_token_file: /var/run/substrate/api-token
    router_url: https://atenet-router.ate-system.svc
    router_ca_file: /var/run/substrate/router-ca.pem
    router_identity_file: /var/run/substrate/router-identity.pem
    egress_policy_file: /etc/omnigent/substrate-egress.yaml
```

Confirm router service/port values against the pinned installation. Keep these
settings in `sandbox.substrate`, not `sandbox.kubernetes`. Reuse top-level
`host_config`, `git_clone`, provider-list configuration, and runner idle timeout.
Use existing launch timeout configuration unless a distinct transport deadline
is necessary. Reject unknown keys and incompatible templates at preflight.

Image, resource sizing, snapshot fidelity, and runtime placement belong in the
ActorTemplate. Avoid redundant Omnigent knobs for them.

## Lifecycle and identity

### Initial launch

1. `prepare()` validates configuration, TLS/authentication, the selected template,
   and the supported bootstrap/snapshot contract without allocating compute.
2. `provision()` reserves an unguessable Actor name. Register the managed host and
   launch-token digest before starting its workload, as current entrypoint-as-host
   providers do.
3. `start_host()` creates the Actor, initially suspended, and installs its
   default-deny egress policy before resuming it. Treat actor creation plus policy
   creation as separate operations; reconcile partial success explicitly.
4. Record the concrete Actor UID against the reserved handle before activation.
   The durable handle must identify the deployment, atespace, Actor name, and UID.
   If existing `started_sandbox_id` persistence is too late, add the smallest
   shared persistence seam needed here rather than an in-memory ownership map.
5. Resume the Actor. Its HTTP wakeup probe reports that bootstrap can receive an
   assignment, not that a session host is already online. This avoids a deadlock
   where readiness waits for activation that cannot be sent until resume returns.
6. Deliver a bounded activation payload through the authenticated router, with
   `ate-target-actor` derived only from the server's stored assignment. Include
   Actor UID, host ID, activation generation, scoped launch token, server URL,
   repositories, and managed host configuration.
7. Bootstrap validates its projected Actor UID, prepares HOME/repositories, and
   starts the ordinary supervised host. Report nonsecret preparation status.
8. The existing managed-host path waits for authenticated online registration;
   the session runner starts over the existing tunnel.

Activation should expose only readiness, activation, and nonsecret status, not
an arbitrary HTTP exec endpoint. Reuse preparation helpers internally. Identical
activation retries must succeed after a lost response; conflicting assignments
must fail. Never put tokens in ActorTemplate environment variables, metadata,
URLs, command argv, diagnostics, or golden snapshots.

The allocation sequence must survive a process crash between every remote call
and local write. A pending handle must remain discoverable for retry/cleanup.
Do not claim UID-safe cleanup merely because the final successful handle has a
UID: explicitly test the create-response-lost and UID-write-failed cases.

### Suspend and wake

For the first release, require a VOLUMES template and set
`resume_stopped=True`, `resume_preserves_host=False`. VOLUMES caps exported
snapshot fidelity; Substrate may still retain processes locally. The provider's
`resume_keeps_host()` restores compute and inspects bootstrap state before the
server rotates credentials.

- `stop()` calls `SuspendActor`. Request synchronous durability, then verify the
  snapshot is durably complete before reporting durable suspension; an already
  suspended Actor can still have an upload pending.
- `resume()` calls `ResumeActor` and waits for bootstrap readiness. Restored HOME
  remains intact. A cold bootstrap waits for activation; a retained host keeps its
  credential, renewed only against its stored digest and sandbox generation.
- For a cold bootstrap, the existing wake path re-arms a new launch token and
  calls `start_host()` to activate the already-existing Actor. It must not create another Actor or wipe
  the workspace. Bootstrap must wait for this new activation on every cold boot.
- Preserve repository edits, untracked files, harness session files, and installed
  dependencies within durable HOME. Writes outside durable volumes and running
  processes are not preserved under this initial contract.
- A failed wake retains the Actor and its data for retry. A crashed Actor is not
  equivalent to a missing Actor. Do not automatically `RevertActor`: reverting
  can discard newer work and replay external effects.

For lifecycle calls, distinguish suspended, running, crashed, missing, forbidden,
and unknown/transient status. Only a definitive missing generation should raise
`SandboxGoneError`. Authentication failures, capacity exhaustion, and timeouts
must not trigger destructive replacement.

### Idle control and concurrency

The current keepalive loop only stops renewing a provider after work becomes
idle. That does not cause Substrate to suspend an Actor. Implement an explicit,
provider-opt-in idle-stop path using existing runner work signals.

- Aggregate all runners and sessions on the managed host. Any active work,
  pending launch, or unknown/stale activity prevents suspension.
- Use the existing runner idle window; reading a chat or receiving host tunnel
  heartbeats does not count as agent work.
- Coordinate suspend, wake, launch, and deletion through a durable per-host
  transition claim/activity generation. Per-replica timers and Python locks are
  not sufficient for a multi-replica deployment.
- Recheck activity under that claim immediately before suspension. New work must
  either cancel the stop or wait for it and resume before dispatch. Include
  automation runs and subagent launches in this ordering.
- Reuse the server's background-task lifecycle. A server restart must not infer
  idle from missing in-memory reports. Prefer temporarily retaining compute over
  suspending work whose status is unknown.

Ship manual/operator suspend-resume in the first vertical slice. Enable automatic
suspension only after the concurrency tests pass. Do not advertise automatic
multiplexing while idle Actors remain resident indefinitely.

### Cleanup and upgrades

- `terminate()` deletes only the recorded Actor generation, using delete
  preconditions where supported. Already absent is success.
- Preserve the existing cleanup retries and provider-matched reaper behavior.
  Never delete shared templates, pools, or atespaces as session cleanup.
- The inspected Resume/Suspend requests identify Actors by name, without a UID
  precondition. Check the recorded UID, never reuse names, and document the
  remaining check/call race with privileged external recreation. Do not present
  a prior read as atomic fencing.
- Version templates and images. New hosts use the new template; existing hosts
  remain pinned until an explicit migration. Keep their provider configuration
  and template available for wake and teardown during rollback.
- Changing the default provider affects new allocations, not existing Actors.
  A rollback must not delete user workspaces or silently migrate them to another
  provider. Export needed work before deliberate Actor deletion.

## Security and transport requirements

### Server-to-Substrate

Use the public Control gRPC schema through a private, bounded client adapter.
Prefer a maintained Python binding if one exists for the selected release;
otherwise generate bindings reproducibly from pinned upstream protos, including
required imports and license notices. Do not hand-maintain partial protobuf
messages or add an HTTP-to-gRPC sidecar just to avoid client generation.

Make dependencies optional and imports lazy. Follow the existing protobuf
compatibility constraints and package-mirror policy in `pyproject.toml` and
`CONTRIBUTING.md`. Add an image variant only if the existing build matrix requires
one for the optional extra.

Use a projected ServiceAccount JWT for Control calls, with the configured
Substrate audience and verified serving CA. Reread rotated tokens; refresh
channels/trust material on certificate rotation. Do not use the installation's
shared bootstrap-owner identity. Grant only the required atespace access.

Router access uses its own mTLS identity/trust and SPIFFE allowlist. The inspected
router allowlist permits access to any Actor, not just this atespace. Keep that
identity on the trusted server, never in the actor or browser, and do not accept
an arbitrary routing header or Actor reference from an end user.

### Actor-to-Omnigent and external services

The host uses outbound WebSockets. Require an HTTPS Omnigent endpoint and an
exact-host/port `tls_passthrough` rule for its WSS traffic. Verify certificate
trust inside the host image, including a private serving CA when applicable.
Do not disable TLS verification or substitute an inspected HTTPS rule.

Define a minimal operator-approved allowlist for Git HTTPS, model endpoints,
package registries, and any remote MCP servers actually needed. No wildcard
internet access by default. Raw SSH Git and arbitrary UDP are not part of the
initial supported contract. Test streaming model responses and WSS reconnects,
not just an HTTPS health request.

Substrate credential injection can keep static inference keys outside the actor
on inspected HTTPS destinations. It cannot inject the Omnigent host token into
a TLS-passthrough WebSocket. The initial integration therefore retains a scoped
Omnigent host credential inside the sandbox, and existing broker flows may also
return short-lived upstream credentials to the actor. Explicitly document this
exception to a fully credential-free design. Snapshot storage can contain
workspace secrets even when activation files are excluded from durable HOME.

Keep server credentials, Kubernetes tokens, and Substrate client credentials out
of ActorTemplates and activations. Preserve existing per-owner broker checks;
an Actor name supplied by an untrusted caller is never evidence of ownership.

## Implementation sequence

Each phase is a focused PR or small dependent set of PRs. Open a tracking issue
before implementation and record the selected Substrate release there.

### 0. Prove compatibility and freeze the contract

- Confirm released schema support for VOLUMES, durable directories, projected
  Actor identity, router activation, TLS passthrough, and authentication.
- Run a disposable non-browser probe: cold boot the host image, activate it,
  establish the real WSS tunnel, suspend, restore HOME, rotate the launch token,
  and reconnect. Verify native harness subprocess/tmux/sandbox compatibility;
  do not disable Omnigent's nested isolation just to make the probe pass.
- Decide the minimal client packaging and activation protocol. Check whether
  image permissions or a small derived image are necessary.

**Exit:** A reproducible transport/lifecycle transcript, a version matrix, and no
unresolved blocker to a real managed session. No performance promise yet.

### 1. Add provider configuration and Control client

Touch the provider registry, `server/managed_hosts.py`, optional dependencies,
and a new `onboarding/sandboxes/substrate.py` module. Keep wire details private.

Implement configuration validation, preflight, reserved/concrete handles,
bounded lifecycle calls, error translation, and idempotent partial-failure
recovery. Persist ownership before activation. Do not mutate infrastructure.

**Exit:** Focused tests cover malformed configuration, absent extras, auth errors,
UID mismatch, lost responses, repeat deletion, and transient capacity failures.

### 2. Add actor bootstrap and complete managed launch

Add a small bootstrap entrypoint under `omnigent/host/`, sharing sufficient
preparation/config/clone helpers with the Kubernetes path. Extract only helpers
actually needed by both implementations. Do not make the Pod-specific warm-pool
protocol pretend to be a Substrate protocol.

Add a deployment template and activation transport. Support empty and multi-repo
workspaces, existing `git_clone` options, host configuration, host slice routing,
and the normal credential brokers. Advertise only implemented capabilities;
leave CLI bootstrap, file copy, and provider exec disabled.

**Exit:** A managed session performs a tool call in the intended Actor, with
correct owner isolation and no activation credentials in control-plane objects
or logs. Failed preparation cannot produce a falsely online host.

### 3. Complete durable wake, idle suspension, and cleanup

Implement VOLUMES wake and fresh activation, then the opt-in idle-stop path and
its durable concurrency control. Extend the existing host store only for state
that must survive process failure or coordinate replicas. Keep any schema change
backward-compatible during a rolling server upgrade.

**Exit:** Repeated suspend/wake preserves workspace edits; concurrent sends,
multiple runners, server restart, deletion, and failed snapshot upload do not
lose work, create duplicate hosts, or leak untracked Actors. New activity cannot
race an idle stop into dispatching work to a suspended host.

### 4. Package the Kubernetes deployment pattern

Add `deploy/kubernetes/overlays/substrate-runners/` and its runbook. Reuse the
existing Kubernetes server base and database patterns. Separate:

1. Operator installation/prerequisites and WorkerPool manifests.
2. Operator ActorTemplate/access-policy creation through Substrate tooling.
3. Omnigent server configuration, credential projections, and network policy.

Do not imply `kubectl apply` can create Control-plane ActorTemplates. Pin images
by verified digest and document runtime-specific node requirements. Include
readiness/preflight diagnostics, snapshot storage, capacity planning, upgrades,
rollback, and workspace retention/deletion semantics.

Update `deploy/README.md`, `deploy/kubernetes/README.md`, and
`docs/extending/sandbox_providers.md`. Check provider discovery in `/v1/info` and
the generic UI selector; add frontend code only if an actual hardcoded list
blocks this provider.

**Exit:** An operator can deploy from the documented prerequisites without
editing application code, and can disable new allocations without stranding
existing sessions.

### 5. Qualify and release as opt-in

Run the focused checks below, then a cluster smoke scenario against the pinned
release. Qualify gVisor first. Run the same scenario on microVM before claiming
support for it. Measure cold launch, snapshot time, wake-to-host-online latency,
and memory/worker occupancy for a mostly-idle workload.

**Exit:** Published supported-runtime/version matrix, known limitations, and
acceptance evidence. Switching providers does not change existing deployments.
Do not repeat upstream latency/density claims as measured Omnigent results.

### Follow-up: memory resume and stronger credential isolation

MEMORY snapshot exports are a separate increment, not a flip of
`resume_preserves_host`. Substrate can downgrade MEMORY to ROOTFS on an
incompatible worker, and changing templates can force a cold boot. Reuse the
implemented bootstrap-state handshake for local retention versus cold boot,
and verify exactly one host across each exported-snapshot fallback. Address tokens
that expire during long suspension, stale sockets, restored timers, and any
persisted activation state before enabling it. Extend the existing
host-preserving wake seam rather than introducing a second wake subsystem.

Evaluate gateway-injected actor JWT authentication for Omnigent and brokers
separately. It needs a compatible tunnel transport or a trusted bridge; ordinary
TLS passthrough cannot provide HTTP header injection. Do not weaken network
policy or host-owner authorization to claim zero credentials.

## Verification and acceptance

Follow [the test guide](../tests/README.md) and
[primitive catalog](../tests/_helpers/README.md). Extend compatible existing
scenarios first; keep input/error matrices at the client/bootstrap layer.

| Layer | Planned coverage |
| --- | --- |
| Provider tests in `tests/onboarding/sandboxes/` | Public launcher contract, strict config, state/error mapping, UID checks, retry/deletion semantics, rotated credentials. |
| Bootstrap tests in `tests/host/` | Actor UID binding, bounded payloads, duplicate/conflicting activation, readiness ordering, no token logging, cold boot waiting for fresh credentials. Reuse `test_warm_bootstrap.py` patterns. |
| Existing `tests/server/test_managed_hosts.py` | Launch/wake ordering, token rotation, concrete-ID persistence, retained data on failed wake, concurrent sends and generation changes. Existing host-preserving tests inform the later MEMORY phase. |
| Existing keepalive/reaper tests | Multiple runners per host, missing activity, suspend/send/delete races, multi-replica claims, failed cleanup retry, restart recovery. |
| Existing server integration patterns | Real host plus fake Control transport and mock model, using explicit owner identities and existing host/session primitives. |
| One non-browser deployed-cluster scenario | Real gRPC authentication, router mTLS/activation, policy-controlled WSS, snapshots/storage, and two-Actor isolation. This distinct transport/runtime boundary cannot be proved by mocks. |

The cluster scenario should create two sessions for different owners, edit a
file and leave an untracked file in one, idle/suspend it, send another message,
and verify the same Actor/host and files return. Also exercise denied egress,
capacity exhaustion, a server rollout, an interrupted launch, and final cleanup.
Use deterministic mock inference for the core scenario; validate each advertised
native harness separately where it adds a distinct process boundary.

### Human acceptance walkthrough after implementation

1. Follow the new overlay runbook on a disposable Substrate installation.
2. Open **New session**, select **Substrate**, and choose a supported harness and
   repository. Ask it to write `substrate-check.txt` containing `before sleep`.
3. Wait past the configured runner idle window. Confirm the Actor is suspended
   with a completed durable snapshot, rather than deleted or merely offline.
4. Send “Read substrate-check.txt and append after wake.” Confirm the same host
   and Actor UID return and the file contains both lines.
5. Repeat while another session on that host is actively working. The Actor must
   remain running until all work is idle.
6. Roll the Omnigent server and repeat the wake. Check that activation secrets
   do not appear in template output, logs, or diagnostics.
7. Delete the managed host through the normal workflow. Confirm eventual Actor
   and actor-owned storage cleanup while shared templates and pools remain.

No local browser automation is required for implementation verification. Browser
checks require an explicit request; the walkthrough above is for human testing.

## Non-goals and remaining decisions

Not included: server-as-Actor deployment, automatic Substrate installation,
per-tool sandboxes, a new Kubernetes operator, a new profile UI, transparent
migration of existing providers, guaranteed preservation of in-flight external
requests, or guaranteed recovery of writes made after the last durable snapshot.

Phase 0 must settle the released version/client bindings and prove image/runtime
compatibility. Phase 3 must settle the durable idle-transition schema and race
protocol before automatic suspension ships. These are implementation gates, not
claims that the existing code already provides those guarantees.
