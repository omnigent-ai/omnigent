# Substrate managed runners (experimental)

Keep the Omnigent server on Kubernetes and run managed hosts inside Agent
Substrate Actors. This overlay targets the Substrate **v1.0.0** Control protocol.
It is an initial implementation, not a production-qualified deployment.

Implemented: managed launch, authenticated activation, VOLUMES snapshot wake with
generation-checked host credentials, cleanup, repository preparation, and
host configuration. **Automatic idle suspension is not implemented.** Use only a
single server replica for this experimental path; distributed suspend/wake
coordination remains a release gate. MEMORY snapshot templates are not supported.
Substrate can retain processes locally even with VOLUMES fidelity; the provider
inspects the restored bootstrap before deciding whether to reuse or restart the host.
See the [implementation plan](../../../../designs/SUBSTRATE_MANAGED_RUNNERS.md).

## Prerequisites

- A Substrate v1.0.0 installation using the Envoy router, with gVisor workers and
  durable snapshot storage. Install Substrate separately using its
  [operator instructions](https://github.com/agent-substrate/substrate/blob/v1.0.0/cmd/ate-setup/README.md).
- Kubernetes support for `ClusterTrustBundle`, `ClusterTrustBundleProjection`,
  and `PodCertificateRequest`, including their served API versions. Stock local
  clusters may not enable these. Substrate's kind setup enables them explicitly.
- Digest-pinned server and host images built from this checkout, using the same
  architecture as the worker pool. No published Omnigent image is assumed to
  already contain this integration.
- An HTTPS Omnigent endpoint reachable from actor egress, with a certificate the
  host image trusts. Follow the [base Kubernetes guide](../../README.md) for
  server authentication, ingress, and storage. Use Postgres before scaling the
  server; this experimental provider still requires one replica.
- An operator identity authorized to create atespaces, templates, access policies,
  and pools. Never use that identity as the server's runtime credential.

MicroVM and native harness compatibility must be qualified on their real
runtimes before claiming support. Do not disable nested sandbox checks to work
around runtime incompatibilities.

## Build and configure

Build the regular server with `OMNIGENT_EXTRAS=substrate` and the regular host
from the repository root, following [the Docker guide](../../../docker/README.md):

```sh
docker build -f deploy/docker/Dockerfile --build-arg OMNIGENT_EXTRAS=substrate \
  -t registry.example.com/omnigent-server-substrate:dev .
docker build -f deploy/docker/Dockerfile --target host \
  -t registry.example.com/omnigent-host-base:dev .
```

Push these images to your registry, resolve their immutable digests, then build
the non-root actor image. `BASE_IMAGE` must name your built host by digest:

```sh
docker build -f deploy/kubernetes/overlays/substrate-runners/Dockerfile.host \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  -t registry.example.com/omnigent-host-substrate:dev .
```

For SDK-based workloads, `Dockerfile.minimal-host` is a smaller alternative:
use your checkout-built **server** digest as `BASE_IMAGE` instead. It adds Git,
tmux, and Bubblewrap, but does not install standalone native-harness CLIs. This
is the profile used for the lifecycle qualification below.

```sh
docker build -f deploy/kubernetes/overlays/substrate-runners/Dockerfile.minimal-host \
  --build-arg BASE_IMAGE="$SERVER_IMAGE_DIGEST" \
  -t registry.example.com/omnigent-host-substrate:dev .
```

Push the chosen image and resolve its digest. Use your actual registry instead
of the reserved example domain. Configure a copy of this checkout's overlay and
`.example` files, keeping its relative reference to the Kubernetes base, then:

1. Replace the server image's `build-required` reference with your built server
   digest. This sentinel deliberately prevents accidentally deploying an old
   image without the extra.
2. Replace `REPLACE_WITH_BUILT_HOST_IMAGE_DIGEST` in `actor-template.yaml.example`
   with the derived host image digest.
3. Review the pinned v1.0.0 `ateworker-gvisor` digest in `workerpool.yaml.example`
   against your Substrate installation. Replace `REPLACE_WITH_IMAGE_ARCHITECTURE`
   with `arm64` or `amd64`, matching the host image and workspace binaries. Resolve
   replacement digests yourself; do not use floating tags in an installation.
4. Set the real Omnigent HTTPS origin in both `sandbox.server_url` and its egress
   rule in `sandbox-config.yaml`. Every TLS passthrough rule requires explicit
   `ports: {numbers: [PORT]}`, including the default HTTPS port 443.
5. Add only the Git/model/package/MCP destinations your workload needs. The
   sample allows only the Omnigent origin and GitHub HTTPS. It does not configure
   a model provider or grant unrestricted internet access.

A TLS passthrough rule is required for Omnigent's outbound WebSocket tunnel.
Inspected HTTPS egress rejects upgrades. If you use inspected HTTPS for model
credential injection, project Substrate's MITM trust bundle into the template
and configure each client to trust it. TLS passthrough cannot inject headers.

## Operator resources and runtime permissions

WorkerPools are Kubernetes resources; ActorTemplates are Control resources.
Create the former with Kubernetes and the latter with `kubectl ate`:

```sh
kubectl apply -f workerpool.yaml
kubectl ate create atespace omnigent-runners
kubectl ate create actor-template -f actor-template.yaml
```

An atespace is distinct from a Kubernetes namespace, even when both use the same
name. Keep this atespace dedicated to this deployment. Never reuse deleted Actor
names or atespace names.

Grant the server ServiceAccount `editor` in this atespace with an operator-owned
access policy:

```yaml
metadata:
  name: default
bindings:
  - role: editor
    members:
      - user:system:serviceaccount:omnigent:omnigent-substrate
```

Apply using `kubectl ate create access-policy -a omnigent-runners -f policy.yaml`.
For an existing policy, read its UID/version and use the upstream update workflow;
do not overwrite unrelated grants. The editor role also permits template changes,
though Omnigent itself never changes operator templates or pools.

Add `spiffe://cluster.local/ns/omnigent/sa/omnigent-substrate` to the Substrate
router's `--ingress-allowed-spiffe-ids`, preserving existing allowed identities.
The inspected router allowlist is cluster-wide, not atespace-scoped. Protect this
server credential accordingly.

The overlay mounts only the audience-bound Control token, serving CA, and router
client certificate. It grants no Pod exec or Secret read permissions to the
server. The actor never receives these Kubernetes/Substrate credentials.

Render and review your configured overlay before applying it:

```sh
kubectl kustomize deploy/kubernetes/overlays/substrate-runners
kubectl apply -k deploy/kubernetes/overlays/substrate-runners
kubectl -n omnigent rollout status deployment/omnigent
```

The server namespace receives `ate.dev/api-access=true`, as required by the
Substrate Control NetworkPolicy. Actor traffic still uses the per-Actor egress
policy. Substrate and your cluster CNI must enforce their documented network
isolation; this overlay is not a replacement for it.

## Verify launch and snapshot wake

1. Create a managed session with provider `substrate`, a supported harness, and
   an empty workspace or HTTPS repository. Check `/v1/info` advertises the
   configured provider.
2. Ask the agent to create `substrate-check.txt` with `before sleep`. Verify the
   tool call actually executes in the Actor, rather than a connected local host.
3. Identify its Actor with `kubectl ate get actors -a omnigent-runners`. Record
   the Actor UID and Omnigent host ID. Wait for all work on that host to finish.
4. Manually suspend the idle Actor:

   ```sh
   kubectl ate suspend actor ACTOR_NAME -a omnigent-runners --mode=sync
   kubectl ate get actor ACTOR_NAME -a omnigent-runners -o yaml
   ```

   Verify suspended state and completed durable snapshot storage. Operator
   suspension does not check Omnigent's runner activity: never do this mid-turn.
5. Once the host reports offline, send “Read substrate-check.txt and append after
   wake.” Omnigent resumes the Actor, then either activates a cold host with a fresh
   token or renews the exact credential of a locally retained host. Check the
   same Actor UID/host ID and both lines in the file. Untracked files and changes
   inside durable HOME should survive. Do not rely on retaining processes or
   writes outside HOME; a cold restore discards them.
6. Repeat with an Omnigent server restart between suspension and wake. Test
   cross-owner access denial, denied egress, and capacity exhaustion.
7. Delete the managed host normally. Verify Actor and actor-owned snapshot
   cleanup, with the shared template and WorkerPool left intact.

Use a deterministic mock model for infrastructure tests. Do not assume a working
HTTPS health check proves WSS or native harness compatibility. No browser
automation is required for the transport scenario.

## Failure handling and operations

- `UNAUTHENTICATED` / `PERMISSION_DENIED`: verify token audience, projected files,
  atespace policy, and router allowlist. These are not treated as missing Actors.
- Activation transport failures: verify router serving CA, client certificate,
  service port (443), and bootstrap listener on Actor port 80. The wake probe
  checks bootstrap readiness, not managed-host online state.
- Preparation failures: inspect repository access and configuration. Activation
  payloads and preparation output are deliberately excluded from diagnostics.
  A failed, exited attempt can be reactivated only for the same host and server.
  An already-preparing/running host keeps its existing credential on wake retries.
- Capacity or transient Control failures: retry after fixing capacity/connectivity.
  Control calls and bootstrap preparation currently have a 120-second deadline;
  use prepared images and appropriate clone depth for larger workloads. A wake
  failure retains the Actor and workspace; it does not delete them.
- Crashed Actors: inspect with Substrate tooling. Do not automatically revert;
  reverting can discard changes and replay external effects.
- A stale handle never deletes an Actor with a different recorded UID. Pending
  allocations are tracked by an unguessable reserved name until the UID is saved.
  An absent, unsettled reservation is retained rather than declared cleaned up:
  a create request may still complete. Late allocation results remain recorded
  even if the host was deleted concurrently. Never externally recreate these
  names. Suspend/resume have no upstream atomic
  UID precondition, so a preceding UID check cannot fence privileged recreation.
- Actor templates must have VOLUMES fidelity and disabled golden snapshots. Do
  not enable memory snapshots: durable memory/rootfs export needs additional
  credential and replay handling. Keep launch credentials out of templates and durable activation
  files. Git and Databricks broker configuration refer to the private, ephemeral
  `/run/omnigent-bootstrap/host-token` file rather than persisting the token in
  HOME or argv. The derived image creates its directory with mode `0700`; do not
  mount a durable volume over it or an ancestor.
  Workspace files can still contain secrets; protect snapshot storage.
- The actor retains a scoped Omnigent host token. Existing credential brokers may
  provide short-lived upstream credentials. This is not a zero-credentials actor
  integration, even if some inference credentials are injected at egress.

Substrate uses SIGTERM to announce worker eviction. Bootstrap keeps the host
alive and exposes `draining: true` in its nonsecret status so it can still be
snapshotted. Synchronously suspend Actors **before** planned worker rollouts,
scale-downs, and node drains. Automatic evacuation is not implemented; an
unhandled eviction can still lose writes since the last durable snapshot.

For upgrades, create a versioned template, then change the configured template
for new hosts. Existing Actors keep their template reference. Retain the old
image/template and provider connection settings until those hosts are retired.
Changing the default provider must not delete existing workspaces. Export needed
files before deliberately deleting an Actor; snapshots are not independent
backups and are collected with their owner.

Native harnesses using a nested OS sandbox must explicitly allow the broker
helper to read `/run/omnigent-bootstrap/host-token` in their existing sandbox
policy. Grant that file only, not all of `/run`. The integration does not silently
widen an agent's filesystem permissions. Test credential helpers inside each
harness before claiming private-repository or brokered-inference support there.

The full multi-harness host image needs substantial node image-cache space after
unpacking. Check free space under `/var/lib/ate` before launch. An image-unpack
failure is a provisioning failure, not evidence that the workspace was deleted.
This overlay does not impose a per-Actor disk quota on `durableDir`; configure
storage capacity, monitoring, and limits appropriate to the Substrate installation.

## Qualification status

Validated on Substrate v1.0.0, gVisor, arm64, and Kubernetes v1.37.0:

- Verified Control TLS with projected, audience-bound credentials and router mTLS.
- A real managed session with both host and runner online over WSS.
- Host filesystem RPCs, synchronous VOLUMES suspension, message-triggered wake,
  retained Actor UID/host ID/workspace marker, and Actor cleanup.
- A non-root, PID-1 actor and nested Bubblewrap: the credential path is inaccessible
  without an explicit read grant and readable with the exact-file grant.
- Linux cleanup of orphaned descendants in separate process sessions.

Focused tests cover malformed configuration, the pinned wire schema, credential
rotation/renewal, activation retries, allocation/deletion races, policy defaults,
and snapshot durability. Kustomize rendering and targeted Python type/lint checks
are also covered. The package mirror could not be reached during local checks.

The deployed smoke uses an isolated server/database and does not qualify model
inference, every native harness, cross-node restoration, microVM, or multi-replica
operation. No browser verification was performed. Automatic idle suspension and
its durable admission/transition protocol remain unimplemented; this is not
production support or a completed implementation of every phase of the plan.

To repeat the deployed lifecycle check, run this from a checkout in the disposable
server test container, with local single-user header auth, its isolated database,
and the server's Substrate credential/config mounts available:

```sh
python -m tests.e2e.integrations.deploy.substrate.e2e_lifecycle \
  --server https://omnigent.example.com \
  --config /etc/omnigent/config.yaml \
  --database-uri sqlite:////data/test-chat.db \
  --agent-id YOUR_REGISTERED_AGENT_ID
```

Use `--ca /path/to/test-ca.pem` for a private test CA. The scenario waits for both
host **and runner** readiness before suspension; an online host alone does not
mean initial runner startup is finished. It sends a message to trigger wake, but
does not assert inference output. Never point it at a shared development database
or production deployment. New E2E setup is limited to this distinct real gRPC,
mTLS/WSS, and snapshot boundary; unit tests retain the input/error matrices.

Do not advertise automatic multiplexing or upstream latency/density figures as
measured Omnigent behavior.
