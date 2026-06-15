# Omnigent on Kubernetes

A reference deployment for running Omnigent on a Kubernetes cluster, in two parts:

- **`server/`** — the Omnigent **server** (the meta-harness hub) as a `Deployment`.
  This is straightforward: per the OSS entrypoint the server runs in
  *external-runners-only* mode (it accepts runner connections at
  `/v1/runner/tunnel` and never spawns harness subprocesses itself), so the pod
  needs **no special privileges** — an ordinary container (no `privileged`, no
  `CAP_SYS_ADMIN`, no host access), unlike the host daemon below.
- **`host/`** — a **stopgap** that runs the `omnigent host` daemon as a
  `Deployment` *on the cluster*, so cluster nodes become agent compute. See the
  gap below.

> Extracted and generalized from a working homelab K3s deployment. Everything
> here is environment-agnostic with placeholders — adapt the namespace, image
> tag, domain, Postgres, auth, storage class, and Ingress to your cluster.

## The gap this addresses (`omnigent-ai/omnigent#39`)

Omnigent already has **managed sandboxes** with a pluggable `sandbox.provider`
(see [`deploy/README.md`](../README.md#run-hosts-in-cloud-sandboxes)) — the
server can provision a sandbox per session and run the agent there. The shipped
providers are **lakebox / Modal / Daytona**. What's missing is a **Kubernetes
sandbox provider**: there's no `sandbox.provider: kubernetes` that spawns runner
Pods on your own cluster. So "run the agents as Pods on my K8s cluster" isn't a
config flag today — that one provider is the gap.

The proper fix is a **server-side Kubernetes sandbox provider** that spawns
runner Pods on demand (via the existing launch-token seam, exactly like the
Modal/Daytona providers do). Until that lands, `host/` is a pragmatic
workaround: it runs the **`omnigent host` daemon** in a pod, which dials the
server's `/v1/runner/tunnel` (**outbound only**) and spawns Claude Code / Codex
runners locally on that node. One Deployment per node you want as compute.

This deployment is offered as a starting point for that work — it proves the
server runs cleanly on K8s and demonstrates the host-on-cluster pattern that a
native Kubernetes provider would replace.

## Architecture

```
  Browser ──HTTP──▶ omnigent server (Deployment, ordinary container)
                       │   external-runners-only
                       │   accepts host/runner WS at /v1/runner/tunnel ◀──┐
                       ├─ Postgres (DATABASE_URL)                         │  outbound only
                       └─ artifact PVC (/data)                           │
                                                                          │
                                            omnigent host (Deployment, host/)
                                            runs the prebaked host image +
                                            spawns Claude/Codex runners locally
```

## Quickstart

Default auth is the built-in **`accounts`** provider (multi-user,
username/password, no external IdP). Omnigent **never auto-generates** an admin
password: for a headless deploy set `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` in
the Secret; otherwise create the first admin via the one-time web setup form on
first visit (`/v1/info` reports `needs_setup` until then). Prefer your own IdP?
See the opt-in OIDC block in `server/deployment.yaml`.

```bash
# 1. Server
kubectl apply -f server/namespace.yaml

# Build the Secret from real values (do NOT apply the example as-is — it's
# placeholders). See server/secret.example.yaml, or use sealed-secrets/SOPS/ESO.
kubectl -n omnigent create secret generic omnigent-secrets \
  --from-literal=database-url='postgresql://user:pass@host:5432/omnigent' \
  --from-literal=accounts-cookie-secret="$(openssl rand -hex 32)"
  # + --from-literal=accounts-admin-password='...'  for a headless first admin

kubectl apply -f server/configmap.yaml           # edit admins/allowed_domains first
kubectl apply -f server/pvc.yaml
kubectl apply -f server/deployment.yaml
kubectl apply -f server/service.yaml
kubectl apply -f server/ingress.example.yaml     # adapt to your ingress controller

# First admin (accounts mode): sign in with the accounts-admin-password you set
# above. If you didn't set one, open the URL and use the one-time web setup form.

# 2. (optional, stopgap) agent compute on the cluster — see host/README.md.
# Build the host Secret from real files (auth_tokens.json MUST come via
# --from-file — a hand-written entry without expires_at reads as expired):
kubectl -n omnigent create secret generic omnigent-host \
  --from-file=auth_tokens.json="$HOME/.omnigent/auth_tokens.json" \
  --from-literal=claude-oauth-token="$(claude setup-token)"
kubectl apply -f host/host.yaml
```

## Notes that bit us (worth knowing)

- **Image is `linux/amd64`-only** → `nodeSelector: kubernetes.io/arch: amd64`.
  The host image can't run on arm64 either (`omnigent==0.1.0` →
  `cel-expr-python` has no aarch64-linux wheel).
- **Alembic migrations run in the entrypoint** — no migration Job/initContainer.
- **`DATABASE_URL`** can be any Postgres; the entrypoint normalizes
  `postgresql://` → `postgresql+psycopg://`.
- **Auth (default = accounts):** set `OMNIGENT_AUTH_ENABLED=1` +
  `OMNIGENT_ACCOUNTS_COOKIE_SECRET` + `OMNIGENT_ACCOUNTS_BASE_URL` (the public
  URL). Omnigent never auto-generates an admin password: set
  `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` (from the Secret) for a headless first
  admin, or create it via the one-time web setup form on first visit.
- **OIDC (opt-in):** Omnigent rejects logins where `email_verified != true`.
  Some IdPs (e.g. Authentik's default OpenID `email` scope mapping) emit
  `email_verified: false` — make your IdP assert `true`.
- **Session TTL**: a connecting *host* authenticates with the JWT from
  `omnigent login`; the default 8h expiry breaks a long-lived host on reconnect.
  Raise `OMNIGENT_ACCOUNTS_SESSION_TTL_HOURS` (or `OMNIGENT_OIDC_SESSION_TTL_HOURS`
  under OIDC) — e.g. 720 = 30d — for unattended hosts.

See `server/deployment.yaml` and `host/host.yaml` for inline detail.
