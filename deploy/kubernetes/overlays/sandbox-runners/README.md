# Kubernetes sandbox runners (on-demand host Pods)

This Kustomize overlay turns on the **`kubernetes`** managed-sandbox provider: a
`host_type: managed` session spawns a **batch/v1 Job** whose child Pod runs
`omnigent host` as its container entrypoint and dials back to the server over the
existing launch-token tunnel. It layers the RBAC + config the provider needs onto
the base server deployment.

## Launch model: entrypoint-as-host

The runner is launched as a **batch/v1 Job** (one Pod, `backoffLimit: 6`). The
Job's child Pod runs `omnigent host` as its container command. An **init
container** prepares the workspace (`mkdir` + optional `git clone`); the **main
container** then runs `omnigent host` under a tiny PID-1 reaper. The host
re-parents runner processes to PID 1, which the reaper reaps; SIGTERM is
forwarded for graceful shutdown.

The launch token is delivered through a **per-Job Kubernetes Secret** referenced
by the Pod's `secretKeyRef` — it never enters the Pod spec, a command line, or
an audit log. The launcher creates that Secret at provision and deletes it
alongside the Job at terminate.

Because the host is **never started by `exec`-ing into an already-running
container**, this provider needs **no `pods/exec` grant** — and avoids the
exec-into-running-container class of runtime issues entirely. The server SA's
rights are the minimum the launcher calls: create/get/delete Jobs,
list/get Pods (to poll the Job's child), get `pods/log` (start-failure
diagnostics only), create/delete Secrets (the per-Job token), and list events.

## Two-namespace, least-blast-radius design

| Namespace | Holds |
|---|---|
| `omnigent` | the server, its DB/PVC, its Secrets, the `omnigent-server` SA |
| `omnigent-sandboxes` | runner Jobs (and their child Pods), the per-Job token Secrets, the harness-creds Secret, the powerless `omnigent-runner` SA, the scoped Role + RoleBinding |

The server SA's Job/Pod/Secret rights are a **namespaced Role** bound
(cross-namespace) to `omnigent-sandboxes` only — so a compromised server can
manage runner Jobs but **cannot** delete the server/DB Pods, read the server's
Secrets, or execute commands inside any Pod. The runner namespace enforces Pod Security `restricted`;
the generated runner Pod is already restricted-compliant (non-root uid 1000, drop
`ALL` caps, `seccompProfile: RuntimeDefault`, no privilege escalation).

## Agent classifier label (`omnigent.ai/agent`)

Each runner Pod is stamped with `omnigent.ai/agent: <name>` naming the built-in
agent its session runs, so an admission policy (or any Pod selector) can tell
which agent a managed runner is running and augment it — the motivating case is
injecting a workload-scoped credential into only the Pods running a given agent.

The value is a **join key you write into your policy**: it equals the agent name
exactly. The label is stamped only when two conditions hold, and is **omitted**
otherwise — the server never emits a mangled or colliding value:

- The session is bound to a **genuine built-in** (operator-seeded) agent. A
  session-scoped agent whose name merely matches a built-in's fails the gate by
  design, so a caller cannot self-classify a runner into another agent's
  identity and attract its credential.
- The agent name is **already a valid Kubernetes label value**. A name that
  would need lossy rewriting is dropped rather than coerced, because two
  distinct names must never collapse onto one credential-selecting value.

### Lifecycle — when a session loses the label

The classifier is re-derived from the bound agent at every launch and relaunch;
it is never persisted. Some ordinary UI actions therefore drop it:

- **Fork** and **switch-agent** mint a fresh *session-scoped* clone of the
  agent. That clone fails the built-in gate, so the forked/switched session's
  runner gets **no** `omnigent.ai/agent` label — and therefore no
  policy-injected credential.
- **Switching back does not restore it.** Switch-back takes the same path and
  mints another session-scoped clone, so a switched session cannot regain the
  label through the UI. Start a new session on the built-in agent instead.
- **A running Pod keeps the previous agent's label until it is replaced.** The
  label is a launch-time snapshot; Pods are not relabelled in place. A changed
  value lands on the next runner Pod (a relaunch after the sandbox dies), not on
  the live one.

Whichever condition fails, the omission is logged — check these first when a
runner Pod unexpectedly carries no credential:

- Failing the built-in gate logs from `resolve_managed_agent_label`
  (`omnigent/server/managed_hosts.py`), e.g. "agent … is not a genuine built-in;
  omitting agent label".
- A name that is not a valid label value logs a `WARNING` from
  `build_job_manifest` (`omnigent/onboarding/sandboxes/kubernetes.py`), e.g.
  "agent … is not a valid omnigent.ai/agent value; runner Pod … stays
  unclassified". Note the gate upstream will already have logged this agent as
  classified, so this is the line that explains the missing label.

### What the label does not do

The server will not stamp a value the session is not entitled to, but the label
is only as trustworthy as the layer that reads it. **A Pod label is an assertion
by whoever created the Pod**, so before keying anything privileged on it:

- **Restrict who can create Pods in the runner namespace.** Any principal with
  `create` (or `patch`) on Pods there can set `omnigent.ai/agent` to any value.
  The server's gate constrains what *the server* stamps, nothing else.
- **Have the webhook verify the creating identity**, not just the label — e.g.
  that `AdmissionReview.request.userInfo.username` is the server's service
  account. Without this, the label alone is forgeable by a namespace-adjacent
  principal.
- **Write the policy fail-closed**: inject *when* the label matches, rather than
  granting a permissive baseline to Pods without one. Resolution is best-effort
  — a transient store error degrades to an unclassified runner — so absence must
  never mean "more access". Note the inverse risk if you key a *restriction* on
  the label: a Pod that loses it also leaves the restricted set, so build
  restrictions as a default-deny base with this label as the allow-exception.
- **Treat a credential as bound to the Pod, not the session.** A mutating
  webhook injects at Pod creation, and `switch-agent` keeps the same runner
  (host and workspace are untouched), so a session that switches from a
  credentialed agent keeps that credential mounted for the Pod's remaining
  lifetime while running the new agent. If that matters, avoid switch-agent for
  credentialed agents or keep the sandbox's idle timeout short.

## Prerequisites

1. **A server image built with the `kubernetes` extra.** The overlay's
   `images:` block already points at the official `omnigent-server-kubernetes`
   variant, which includes it — nothing to build. If you self-build instead,
   keep `kubernetes` in `OMNIGENT_EXTRAS` (see `deploy/docker`) or
   `_ensure_sdk()` fails every launch, and point `images:` at your build.
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

> **The `secret_name` Secret must exist before the first managed launch.** Its
> `envFrom` is non-optional, so a runner Pod whose Secret is missing never starts
> — it stalls in `CreateContainerConfigError` rather than launching without
> credentials. Create it (step 2) right after the `kubectl apply -k` in step 1.

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
| `provider` | `kubernetes` (a Job with a fixed 7-day cap) or `agent_sandbox` (an agent-sandbox `Sandbox` that reclaims itself once idle): see [Self-reclaiming sandboxes](#self-reclaiming-sandboxes-provider-agent_sandbox). Both read this same `kubernetes:` block. |
| `server_url` | URL the runner Pod's host dials back to (in-cluster service DNS by default). |
| `host_config` | Optional, top-level under `sandbox:` (provider-agnostic, not inside `kubernetes:`): verbatim in-sandbox `~/.omnigent/config.yaml` content installed before `omnigent host` starts — e.g. a `providers:` block routing the `pi` harness through a self-hosted gateway (LiteLLM/vLLM). Server-managed: entries injected by a previous launch are replaced or removed on the next launch/resume; config created inside the sandbox survives. Keep secrets out via `api_key_ref: env:VAR`, resolved inside the runner Pod against the `secret_name` Secret. Validated at server startup. |
| `namespace` | Runner-Pod namespace (defaults to `omnigent-sandboxes`). |
| `secret_name` | Harness-creds Secret projected into every Pod via `envFrom`. |
| `service_account` | ServiceAccount the runner Pods run as (powerless). |
| `image` | Optional runner image override (defaults to the official multi-arch amd64/arm64 host image). |
| `env` | Optional list of SERVER env-var names to inject as literal Pod env (prefer `secret_name` for credentials). |
| `node_selector` | Optional extra node labels, merged with a default `kubernetes.io/arch: amd64` — set that key to `arm64` to schedule runners on arm64 nodes. |
| `resources` | Optional `requests` / `limits` (`cpu` / `memory`) override. |
| `pod_ready_timeout_s` | Optional override for how long to wait for a runner Pod to reach `Running` (schedule + image pull + init container) before failing the launch — default 90s. Raise it for deployments whose host image regularly takes longer to pull. Also settable via env: `OMNIGENT_K8S_POD_READY_TIMEOUT_S` (this key takes precedence when both are set). |
| `in_cluster` | Optional cluster-config source: `true` (in-cluster SA only), `false` (kubeconfig only), omit (try in-cluster, then kubeconfig). |
| `kubeconfig` | Optional kubeconfig path for the out-of-cluster fallback (env: `OMNIGENT_KUBERNETES_KUBECONFIG`). |
| `pvc_mounts` | Optional pre-created PersistentVolumeClaims mounted into every runner Pod — see [Persistent storage mounts](#persistent-storage-mounts-pvc_mounts). |

## Self-reclaiming sandboxes (`provider: agent_sandbox`)

`provider: kubernetes` runs each sandbox as a `batch/v1` Job capped by
`activeDeadlineSeconds` (7 days). That is a *fixed* lifetime, so an abandoned
sandbox holds a node slot for a week whether or not anything ever ran in it.

`provider: agent_sandbox` runs the identical Pod inside an
[agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) `Sandbox`
custom resource, whose `spec.shutdownTime` is a deadline the owner keeps pushing
forward. The server refreshes it for as long as the sandbox has a live runner
tunnel, so:

- a busy sandbox lives as long as work keeps arriving;
- an idle one is reclaimed within one window (default 1h, env
  `OMNIGENT_AGENT_SANDBOX_SHUTDOWN_WINDOW_S`);
- the agent-sandbox controller does the teardown, so the Omnigent server never
  enumerates or babysits live Pods.

Switching is one line: every other key stays in the same `kubernetes:` block:

```yaml
sandbox:
  provider: agent_sandbox      # was: kubernetes
  server_url: http://omnigent.omnigent.svc.cluster.local
  kubernetes:
    namespace: omnigent-sandboxes
    secret_name: omnigent-creds
```

Prerequisites:

1. Install the agent-sandbox controller and its CRDs in the cluster.
2. The `sandboxes` RBAC rule in `role.yaml` (already applied by this overlay).

### Suspend and resume

`shutdownPolicy` is `Retain`, so a lapsed deadline **suspends** the sandbox: the
Pod is torn down, the `Sandbox` object and its volumes stay. Sending a new
message in the session wakes it under the same sandbox id (the server's normal
resume path), and the sandbox keeps its host identity and conversation.

Deleting for good stays explicit: deleting the session terminates it, and a
deployment-wide reaper handles sandboxes abandoned long-term. `kubectl get
sandbox -n omnigent-sandboxes` shows suspended ones with `Ready=False`,
`Reason=SandboxExpired`.

### Durable workspace (`OMNIGENT_AGENT_SANDBOX_WORKSPACE_SIZE`)

By default `$HOME` is an `emptyDir` and dies with the Pod, so a woken sandbox
starts from an empty workspace. Set a size on the **server** to move `$HOME`
onto a per-sandbox PersistentVolumeClaim instead:

```yaml
env:
  - name: OMNIGENT_AGENT_SANDBOX_WORKSPACE_SIZE
    value: "20Gi"
  - name: OMNIGENT_AGENT_SANDBOX_STORAGE_CLASS   # optional
    value: "fast-ssd"                            # omit for the default class
```

The claim covers all of `$HOME`, so the workspace, `~/.omnigent` and harness
caches all survive a suspend. Requirements and caveats:

- The cluster needs a StorageClass with a dynamic provisioner that supports
  `ReadWriteOnce` (kind ships `standard`). Without one the Pod stays `Pending`
  on an unbound claim.
- One claim per sandbox, named `home-<sandbox-id>`, owned by the `Sandbox`, so
  `terminate` (session delete) cascades it away. Nothing is shared between
  sessions.
- `volumeClaimTemplates` is immutable after a `Sandbox` is created, so enabling
  or resizing this applies to **new** sandboxes only; existing ones keep the
  workspace they were born with.
- This is a different lane from `pvc_mounts` below, which stays what it is:
  pre-created, deployment-global, shared, and rejected at/over `$HOME`. Use
  `pvc_mounts` for shared datasets and caches, this for per-session work.

### Warm pools are not usable yet

The extension CRDs (`SandboxWarmPool` / `SandboxClaim`) look like the answer to
slow image pulls, but they cannot help Omnigent as the API stands: a
`SandboxClaim` that sets either `env` or `volumeClaimTemplates` is documented
(and tested upstream) to force a **cold start**, and Omnigent needs both, a
per-session host identity in `env` and a per-session workspace claim. A warm
pod's pre-created volumes are also pool-owned and recycled across claims, which
is not acceptable for sandbox isolation. Pre-pulling the host image onto nodes
is the workaround until upstream can inject per-claim identity without
discarding the warm pod.

## Persistent storage mounts (`pvc_mounts`)

Runner Pods are ephemeral by design — the workspace lives on an `emptyDir` and
dies with the Pod. To expose durable data (datasets, model caches, shared
output directories) mount pre-created PersistentVolumeClaims:

1. Create the PV/PVC **in the runner namespace** (`omnigent-sandboxes`) out of
   band — via your GitOps repo, with whatever backend your cluster provides
   (NFS/SMB CSI drivers, SAN, cloud disks). Omnigent only references the claim;
   it never creates volumes, so the server RBAC stays unchanged.
2. List the claims under `sandbox.kubernetes.pvc_mounts` (see
   `sandbox-config.yaml`). Mount paths may not overlap `/home/omnigent`, the
   OS directories, or their ancestors (e.g. `/home`, `/var`) — the server
   rejects such config at startup.

Caveats:

- **Multiple runners share writable claims concurrently** — use a
  `ReadWriteMany`-capable backend (NFS/SMB/CephFS) for anything writable, and
  prefer `read_only: true` (the default) everywhere else: a writable shared
  mount lets one session's agent read and modify what another session wrote,
  and anything written there outlives the Pod and its launch token.
- Runner Pods run as uid/gid 1000660000 with `fsGroup`. NFS `root_squash` and
  SMB ownership mapping must permit that identity (export to the uid, or use
  CSI mount options like `uid=`/`gid=` for SMB); `fsGroupChangePolicy:
  OnRootMismatch` avoids re-chowning large exports on every start.
- `ReadWriteOnce` claims pin all runners to one node — combine with
  `node_selector` deliberately, or the second Pod sits `Pending`.
- A mount visible in the Pod is not automatically visible to a harness's own
  OS-level sandbox (OmniBox path grants are separate).

To verify `host_config` end to end against a live cluster, run
`python tests/e2e/integrations/deploy/kubernetes/e2e_managed_host_config.py
--server <url>` — it creates a managed session and asserts the injected
config inside the runner Pod.

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
- **Runner Pod stuck in `CreateContainerConfigError`:** the `secret_name` Secret
  (`omnigent-creds`) doesn't exist in the runner namespace — its `envFrom` is
  non-optional, so the Pod can't start. Create it (see [Apply](#apply)).
- **Host comes online but the session hangs / 403s on the first message:** the
  server is using the built-in `accounts` provider, which doesn't support the
  managed runner dial-back — see [Server auth](#server-auth-managed-hosts) (use
  header/OIDC auth, or run single-user).
- **401 / "could not load Kubernetes configuration":** out of cluster, the server
  can't find a kubeconfig — set `kubeconfig` (or `OMNIGENT_KUBERNETES_KUBECONFIG`),
  or unset `in_cluster: true` if it isn't actually running in the cluster.
