# Kubernetes sandbox runners (on-demand host Pods)

This Kustomize overlay turns on the **`kubernetes`** managed-sandbox provider: a
`host_type: managed` session spawns one **runner Pod** that runs `omnigent host`
as its container entrypoint and dials back to the server over the existing
launch-token tunnel. It layers the RBAC + config the provider needs onto the
base server deployment.

## Launch model: entrypoint-as-host

The runner Pod's container command **is** the host. An **init container**
prepares the workspace (`mkdir` + optional `git clone`); the **main container**
then runs `omnigent host` under a tiny PID-1 reaper. The host re-parents runner
processes to PID 1, which the reaper reaps; SIGTERM is forwarded for graceful
shutdown.

The launch token is delivered through a **per-Pod Kubernetes Secret** referenced
by the Pod's `secretKeyRef` — it never enters the Pod spec, a command line, or
an audit log. The launcher creates that Secret at provision and deletes it
alongside the Pod at terminate.

Because the host is **never started by `exec`-ing into an already-running
container**, this provider needs **no `pods/exec` grant** — and avoids the
exec-into-running-container class of runtime issues entirely. The server SA's
rights are the minimum the launcher calls: create/get/delete Pods, get
`pods/log` (start-failure diagnostics only), create/delete Secrets (the per-Pod
token), and list events.

## Two-namespace, least-blast-radius design

| Namespace | Holds |
|---|---|
| `omnigent` | the server, its DB/PVC, its Secrets, the `omnigent-server` SA |
| `omnigent-sandboxes` | runner Pods, the per-Pod token Secrets, the harness-creds Secret, the powerless `omnigent-runner` SA, the scoped Role + RoleBinding |

The server SA's Pod/Secret rights are a **namespaced Role** bound (cross-namespace)
to `omnigent-sandboxes` only — so a compromised server can manage runner Pods but
**cannot** delete the server/DB Pods, read the server's Secrets, or execute
commands inside any Pod. The runner namespace enforces Pod Security `restricted`;
the generated runner Pod is already restricted-compliant (non-root uid 1000, drop
`ALL` caps, `seccompProfile: RuntimeDefault`, no privilege escalation).

## Prerequisites

1. **A server image built with the `kubernetes` extra.** The base image omits
   it, so `_ensure_sdk()` would fail every launch. Build with
   `--build-arg OMNIGENT_EXTRAS=kubernetes` (see `deploy/docker`) and set the
   image in `kustomization.yaml` (`images:` → `newName`/`newTag`).
2. **Harness credentials.** Edit `runner-credentials.yaml` with your real LLM /
   git credentials (or point `secret_name` at a sealed-secret / external-secrets
   managed Secret). The placeholder values are not usable as-is.

## Apply

```sh
kubectl apply -k deploy/kubernetes/overlays/sandbox-runners
```

This creates the runner namespace, both ServiceAccounts, the scoped Role +
RoleBinding, the harness-creds Secret, and the server `sandbox:` config, and
patches the server Deployment to run as `omnigent-server` with the config
mounted.

## Configuration (`sandbox-config.yaml`)

| Key | Meaning |
|---|---|
| `server_url` | URL the runner Pod's host dials back to (in-cluster service DNS by default). |
| `namespace` | Runner-Pod namespace (defaults to `omnigent-sandboxes`). |
| `secret_name` | Harness-creds Secret projected into every Pod via `envFrom`. |
| `service_account` | ServiceAccount the runner Pods run as (powerless). |
| `image` | Optional runner image override (defaults to the official amd64 host image). |
| `env` | Optional list of SERVER env-var names to inject as literal Pod env (prefer `secret_name` for credentials). |
| `node_selector` | Optional extra node labels, merged with the mandatory `kubernetes.io/arch: amd64`. |
| `resources` | Optional `requests` / `limits` (`cpu` / `memory`) override. |
| `in_cluster` | Optional cluster-config source: `true` (in-cluster SA only), `false` (kubeconfig only), omit (try in-cluster, then kubeconfig). |
| `kubeconfig` | Optional kubeconfig path for the out-of-cluster fallback (env: `OMNIGENT_KUBERNETES_KUBECONFIG`). |

## Troubleshooting

- **Launch fails fast with a clear reason.** When a Pod can't schedule, pull its
  image, or clone its repo, the launch error carries the diagnosis — recent Pod
  events and a tail of the failed container's log (e.g. the `git clone` error
  from the init container). No need to catch the Pod before it's reaped.
- **Inspect a stuck launch:** `kubectl describe pod <pod> -n omnigent-sandboxes`
  and `kubectl logs <pod> -n omnigent-sandboxes -c host` (or `-c workspace-prep`
  for the clone step).
- **403 on launch:** the server SA is missing the Role — re-apply this overlay
  and confirm the cross-namespace RoleBinding subject namespace is `omnigent`.
- **401 / "could not load Kubernetes configuration":** out of cluster, the server
  can't find a kubeconfig — set `kubeconfig` (or `OMNIGENT_KUBERNETES_KUBECONFIG`),
  or unset `in_cluster: true` if it isn't actually running in the cluster.
