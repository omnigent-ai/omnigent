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
2. **Harness credentials.** The runners read their LLM / git credentials from a
   Secret named by `secret_name` (default `omnigent-creds`); you create it out of
   band after applying the overlay — see step 2 of **Apply**. It is deliberately
   not checked in; for production prefer a sealed-secret / external-secrets Secret.

## Apply

```sh
# 1. RBAC, the runner namespace, the server sandbox config, and the Deployment patch.
kubectl apply -k deploy/kubernetes/overlays/sandbox-runners

# 2. The harness-credentials Secret the runners read — created out of band, like
#    the OIDC secret in ../../README.md. Add only the keys your agents use.
kubectl create secret generic omnigent-creds -n omnigent-sandboxes \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=OPENAI_API_KEY=sk-...
```

Step 1 creates the runner namespace, both ServiceAccounts, the scoped Role +
RoleBinding, and the server `sandbox:` config, and patches the server Deployment
to run as `omnigent-server` with the config mounted. Step 2 supplies the model /
git credentials — see [Model credentials](#model-credentials-llm-keys) and
[Git credentials](#git-credentials-private-repositories) below for which keys to
set (and a sealed-secret / external-secrets operator for production).

## Server auth (managed hosts)

There are two kinds of credential here: the **server-connection** auth below, and
the **model** keys in the next section — keep them separate.

A managed sandbox opens two connections back to the server. The **host tunnel** is
authenticated by the per-launch token directly — the per-Pod token Secret, always
works. But each session's **runner tunnel**, opened by the runner the host spawns,
authenticates with whatever *server* credential it can resolve — **not** the host
token. So how you front the server matters:

- **Header / OIDC-proxy auth, or single-user (no-auth) servers** — the runner
  tunnel needs no extra identity; managed hosts work out of the box. (Verified
  end-to-end on a header-auth server: a `host_type: managed` session launched a
  runner Pod and ran a Claude turn on an injected `CLAUDE_CODE_OAUTH_TOKEN`.)
- **The built-in `accounts` provider (`OMNIGENT_AUTH_ENABLED=1`)** — the runner
  tunnel additionally requires a *user* identity, which the per-launch host token
  does not carry, so the runner dial-back is refused (`403`) even though the host
  tunnel connects. This is a framework-level managed-host interaction shared by
  **all** sandbox providers (Modal / Daytona / Islo / …), not specific to Kubernetes.

So front the server with **header or OIDC auth** — a reverse proxy / IdP injects
the user identity on every request, including the runner WebSocket (see
[`deploy/README.md`](../../../README.md#auth)) — or run it single-user.

## Model credentials (LLM keys)

A fresh runner Pod has no model keys. They ride the **`omnigent-creds` Secret**
(`secret_name`, projected into every Pod via `envFrom`) created in [Apply](#apply);
the in-sandbox host forwards the standard harness credential vars to its runners.
Which variables to inject — first-party APIs, gateways (`*_BASE_URL`),
subscriptions — is identical to Modal; see the [variable table and per-plan
recipes](../../../modal/README.md#llm-credentials-for-managed-sandboxes). For a
Claude **subscription**, run `claude setup-token` on your own machine (one-time
browser auth) and inject the long-lived token as `CLAUDE_CODE_OAUTH_TOKEN`. For
env vars beyond the standard harness set, also set
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=NAME1,NAME2`.

## Git credentials (private repositories)

Inject an HTTPS token as `GIT_TOKEN` (GitLab: add `GIT_USERNAME=oauth2`) into the
`omnigent-creds` Secret. The host image's git credential helper answers HTTPS auth
from it for both the launch-time clone and the agent's later `fetch` / `push`,
writing nothing to disk — use HTTPS repository URLs. Details by provider match the
[Modal git guide](../../../modal/README.md#git-credentials-private-repositories).

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
- **Host comes online but the session hangs / 403s on the first message:** the
  server is using the built-in `accounts` provider, which doesn't support the
  managed runner dial-back — see [Server auth](#server-auth-managed-hosts) (use
  header/OIDC auth, or run single-user).
- **401 / "could not load Kubernetes configuration":** out of cluster, the server
  can't find a kubeconfig — set `kubeconfig` (or `OMNIGENT_KUBERNETES_KUBECONFIG`),
  or unset `in_cluster: true` if it isn't actually running in the cluster.
