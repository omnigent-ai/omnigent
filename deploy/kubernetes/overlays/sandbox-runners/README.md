# On-demand runner Pods — kubernetes sandbox provider

This overlay turns the cluster itself into Omnigent's agent compute: instead of
registering a long-lived external host, the server spawns a **runner Pod on
demand** for each `host_type="managed"` session and deletes it when the session
ends. It uses the same managed launch-token seam as the Modal and Daytona
sandbox providers — no per-host browser login, credentials never enter the
sandbox.

It layers on `../../base` (the server Deployment/Service/Ingress/PVC are
unchanged) and adds only what the provider needs:

- **`serviceaccount-server.yaml`** — the `omnigent-server` ServiceAccount (in the
  server namespace `omnigent`); the deployment patch runs the server as it.
- **`namespace-sandboxes.yaml` / `serviceaccount-runner.yaml` /
  `runner-credentials.yaml` / `role.yaml` / `rolebinding.yaml`** — the dedicated
  runner namespace `omnigent-sandboxes` and everything that lives in it: a
  deliberately powerless `omnigent-runner` ServiceAccount for the runner Pods,
  the `omnigent-creds` harness Secret, a namespaced Role granting exactly what
  the launcher calls (`pods` create/get/delete, `pods/exec` get+create, `events`
  list), and a **cross-namespace** RoleBinding granting that Role to the
  `omnigent-server` SA over in `omnigent`. (One resource per file, per the repo's
  manifest convention.)
- **`sandbox-config.yaml`** — a `config.yaml` with the `sandbox: provider:
  kubernetes` section (in `omnigent`), mounted at `/etc/omnigent` (the deployment
  patch sets `OMNIGENT_CONFIG` to it). The server reads the managed-sandbox
  backend from this file, not from env; it points `sandbox.kubernetes.namespace`
  at `omnigent-sandboxes`.
- **`deployment-patch.yaml`** — runs the server as `omnigent-server` and mounts
  the config.

## Two-namespace least-blast-radius design

Runner Pods run in a **separate namespace** (`omnigent-sandboxes`) from the
server, DB and Secrets (`omnigent`). The server SA's `pods` create/get/delete +
`pods/exec` rights are scoped — via the Role + a cross-namespace RoleBinding — to
`omnigent-sandboxes` **only**. So even a fully compromised server can manage
runner Pods but **cannot** exec into or delete the server/DB Pods, and **cannot**
create a Pod that mounts the server namespace's Secrets — the blast radius of the
exec/create grant is contained to disposable runner Pods.

The binding is cross-namespace because Kubernetes RBAC lets a RoleBinding in one
namespace name a subject (the server SA) in another: `role.yaml` + the
`rolebinding.yaml` live in `omnigent-sandboxes`, and the RoleBinding's subject is
`ServiceAccount/omnigent-server` with `namespace: omnigent` set explicitly.
Runner Pods reach the server's in-cluster Service across namespaces via its
fully-qualified DNS name (`omnigent.omnigent.svc.cluster.local`).

## Requirements

- **A server image BUILT WITH the `kubernetes` extra (required — the base image
  will NOT work).** The base server image ships no managed-sandbox extras, so the
  provider's `_ensure_sdk()` would fail on every launch. Build the server image
  with `docker build --build-arg OMNIGENT_EXTRAS=kubernetes`
  (`deploy/docker/Dockerfile`) — or otherwise ensure `pip install
  'omnigent[kubernetes]'` is in the server image — then point this overlay at it:
  the `images:` override in `kustomization.yaml` defaults to a
  `ghcr.io/REPLACE_ME/omnigent-server:kubernetes` placeholder you MUST replace
  with your built image (or `kubectl apply` will pull a nonexistent image).
- **amd64 nodes.** The prebaked host image is amd64-only (`cel-expr-python` has
  no aarch64 wheel), so the launcher always sets `nodeSelector:
  kubernetes.io/arch: amd64` on runner Pods. Make sure the cluster has schedulable
  amd64 nodes.
- **A Bun-compatible kernel on those nodes.** The agent harness runs on Bun,
  whose JSC garbage collector segfaults at startup on some newer Linux kernels
  (see [Troubleshooting](#troubleshooting)). If your amd64 nodes span a mix of
  kernels, label the known-good ones and pin runner Pods to them via
  `sandbox.kubernetes.node_selector`.
- A Postgres database for the server (as for the base deploy).

## Deploy

1. Set `DATABASE_URL` + cookie secret in `base/secret.yaml` (see the
   [base README](../../README.md#deploy-with-an-external-database)), or use the
   `postgres` overlay's DB and reference it here.
2. Edit **`runner-credentials.yaml`** — real harness credentials, drop the keys
   you don't use. (Prefer a sealed-secret / external-secrets operator in prod.)
3. Edit **`sandbox-config.yaml`** — set `server_url` (in-cluster service DNS is
   the default and usually correct), and optionally `image` / `node_selector`.
4. Apply:

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/sandbox-runners/ | kubectl apply -f -
   ```

## How it works

A new chat that requests a managed sandbox triggers, server-side:

1. `provision()` creates a runner Pod (`sleep infinity` under a tiny PID-1
   reaper, `runAsUser: 1000`, writable `HOME` on an emptyDir,
   `automountServiceAccountToken: false`, harness creds via `envFrom`,
   `nodeSelector: kubernetes.io/arch: amd64`) in `omnigent-sandboxes` and waits
   for it to be ready. The wait is **patient on recoverable conditions** —
   `Pending`/`Unschedulable` (so cluster-autoscaler/Karpenter scale-up works),
   `ImagePullBackOff`/`ErrImagePull` (so kubelet pull retries / cold pulls
   succeed) and transient apiserver errors are polled until the deadline — and
   **fast-fails only on truly terminal states** (Pod `Failed`, the host
   container exiting early, or non-self-healing config errors like
   `CreateContainerConfigError`/`InvalidImageName`). On a deadline timeout it
   surfaces the latest scheduler/kubelet events and the current reason.
2. The server execs `omnigent host` into the Pod (`pods/exec`); the host dials
   back over the launch-token tunnel and registers.
3. The agent runs in the Pod. On session end (or relaunch), `terminate()`
   deletes the Pod.

**Supported agent classes:** `claude-sdk` and `codex` — parity with the Modal
and Daytona providers. Terminal / native-ui agents are out of scope (they need a
`bwrap` sandbox an unprivileged Pod can't provide).

**In-cluster vs out-of-cluster.** Running in-cluster (the default here), the
launcher authenticates to the API with the `omnigent-server` ServiceAccount
token — no kubeconfig needed. To drive a cluster from a server running outside
it, set `OMNIGENT_KUBERNETES_KUBECONFIG` to a kubeconfig path instead.

## Troubleshooting

- **Session hangs / host never comes online.** Find the runner Pod — runner Pods
  live in `omnigent-sandboxes`, not the server namespace
  (`kubectl get pods -n omnigent-sandboxes -l omnigent.ai/role=sandbox-host`, or
  watch `kubectl get pods -n omnigent-sandboxes -w` after starting a chat) — and
  read the host log:
  `kubectl exec -n omnigent-sandboxes <pod> -- cat /tmp/omnigent-host.log`.
- **`pods "..." is forbidden`** — the server isn't running as `omnigent-server`,
  the cross-namespace Role/RoleBinding wasn't applied, or it's in the wrong
  namespace. Confirm
  `kubectl get rolebinding omnigent-sandbox-manager -n omnigent-sandboxes -o yaml`
  and check its subject is `omnigent-server` in `omnigent`.
- **Pod stuck `Pending` / `Unschedulable`** — usually no schedulable amd64 node
  (check taints / `kubectl get nodes -L kubernetes.io/arch`); the launcher waits
  this out until its readiness deadline (autoscalers are meant to scale up while
  the Pod is Pending) and, on timeout, surfaces the scheduler event in the
  session error.
- **`ImagePullBackOff`** — the runner image isn't pullable on the amd64 nodes
  (private registry needs an imagePullSecret; set `image` to a reachable ref).
  The launcher retries the pull until its readiness deadline (cold pulls /
  transient registry/cred flaps recover) before failing with the pull event.
- **Agent auth failures inside the Pod** — a key is missing from
  `omnigent-creds`. (Note: the reserved-name rejection of `HOME` /
  `IS_SANDBOX` applies only to direct `sandbox.kubernetes.env` entries, which
  the launcher sets itself; Secret keys mounted via `envFrom` are not
  validated, so avoid putting `HOME`/`IS_SANDBOX` in `omnigent-creds`.)
- **Agent turns crash with a Bun segfault (`embedder failed to suspend thread
  … panic: Segmentation fault`).** The Pod provisions and the host registers
  fine, but the first agent turn fails and the session goes to `failed` with that
  error. This is an upstream Bun/JSC garbage-collector incompatibility with some
  newer Linux kernels (reproduced on `7.0.0` / Ubuntu 26.04; works on `6.8` /
  Ubuntu 24.04), and it is **independent of the seccomp profile** (confirmed with
  both `RuntimeDefault` and `Unconfined`). Fix by pinning runner Pods to nodes on
  a known-good kernel: label them
  (`kubectl label node <node> omnigent.ai/runner-ready=true`) and set
  `sandbox.kubernetes.node_selector: {omnigent.ai/runner-ready: "true"}` (see
  `sandbox-config.yaml`). Inspect node kernels with `kubectl get nodes -o wide`.
  Longer term, a host image built on a Bun version with the kernel fix removes the
  constraint.
