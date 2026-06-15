# Omnigent on Kubernetes

A reference deployment for running Omnigent on a Kubernetes cluster, in two parts:

- **`server/`** — the Omnigent **server** (the meta-harness hub) as a `Deployment`.
  This is straightforward: per the OSS entrypoint the server runs in
  *external-runners-only* mode (it accepts runner connections at
  `/v1/runner/tunnel` and never spawns harness subprocesses itself), so the pod
  is **unprivileged**.
- **`host/`** — a **stopgap** that runs the `omnigent host` daemon as a
  `Deployment` *on the cluster*, so cluster nodes become agent compute. See the
  gap below.

> Extracted and generalized from a working homelab K3s deployment. Everything
> here is environment-agnostic with placeholders — adapt the namespace, image
> tag, domain, Postgres, OIDC, storage class, and Ingress to your cluster.

## The gap this addresses (`omnigent-ai/omnigent#39`)

Omnigent does **not** auto-discover Kubernetes nodes as agent compute, and the
server's managed-sandbox backends are lakebox / Modal / Daytona — **there is no
Kubernetes backend yet**. So "run the agents on my K8s cluster" isn't a config
flag today; it's the runner-on-K8s gap.

The proper fix is a **server-side Kubernetes sandbox launcher** that spawns
runner Pods on demand (via the existing launch-token seam). Until that lands,
`host/` is a pragmatic workaround: it runs the **`omnigent host` daemon** in a
pod, which dials the server's `/v1/runner/tunnel` (**outbound only**) and spawns
Claude Code / Codex runners locally on that node. One Deployment per node you
want as compute.

This deployment is offered as a starting point for that work — it proves the
server runs cleanly on K8s and demonstrates the host-on-cluster pattern that a
native launcher would replace.

## Architecture

```
  Browser ──OIDC──▶ omnigent server (Deployment, unprivileged)   ◀── host(s) dial in
                       │   external-runners-only                     (/v1/runner/tunnel,
                       │   /v1/runner/tunnel  ◀──────────────────┐    outbound only)
                       ├─ Postgres (DATABASE_URL)                │
                       └─ artifact PVC (/data)             omnigent host (Deployment, host/)
                                                           runs the agentic-dev toolchain +
                                                           spawns Claude/Codex runners locally
```

## Quickstart

```bash
# 1. Server
kubectl create namespace omnigent
# edit server/secret.example.yaml (DATABASE_URL + OIDC) -> apply as a real Secret
kubectl apply -f server/secret.example.yaml      # or your sealed-secrets/SOPS/ESO equivalent
kubectl apply -f server/configmap.yaml
kubectl apply -f server/pvc.yaml
kubectl apply -f server/deployment.yaml
kubectl apply -f server/service.yaml
kubectl apply -f server/ingress.example.yaml     # adapt to your ingress controller

# 2. (optional, stopgap) agent compute on the cluster — see host/README.md
kubectl apply -f host/secret.example.yaml        # the host's session + harness tokens
kubectl apply -f host/host.yaml
```

## Notes that bit us (worth knowing)

- **Image is `linux/amd64`-only** → `nodeSelector: kubernetes.io/arch: amd64`.
  The host bootstrap also can't run on arm64 (`omnigent==0.1.0` →
  `cel-expr-python` has no aarch64-linux wheel).
- **Alembic migrations run in the entrypoint** — no migration Job/initContainer.
- **`DATABASE_URL`** can be any Postgres; the entrypoint normalizes
  `postgresql://` → `postgresql+psycopg://`.
- **OIDC `email_verified`**: Omnigent rejects logins where `email_verified != true`.
  Some IdPs (e.g. Authentik's default OpenID `email` scope mapping) emit
  `email_verified: false` — make your IdP assert `true`.
- **Session TTL**: a connecting *host* authenticates with the JWT from
  `omnigent login`; the default 8h expiry breaks a long-lived host on reconnect.
  Raise `OMNIGENT_OIDC_SESSION_TTL_HOURS` (e.g. 720 = 30d) for unattended hosts.

See `server/deployment.yaml` and `host/host.yaml` for inline detail.
