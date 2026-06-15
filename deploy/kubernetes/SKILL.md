---
name: deploy-kubernetes
description: Deploy the Omnigent server to a Kubernetes cluster (Deployment + Service + Ingress + PVC + Postgres-via-DATABASE_URL), and optionally run agent compute on the cluster via the `omnigent host` stopgap. Invoke when the user wants to apply the manifests, adapt them to their cluster (namespace, image tag, domain, auth, storage class, ingress controller), debug the server or host pod, or work on the Kubernetes-sandbox-provider gap (omnigent-ai/omnigent#39).
---

# Run Omnigent on Kubernetes

The manifests here deploy the Omnigent **server** — the FastAPI / WebSocket
coordinator from the shared `deploy/docker/Dockerfile` (`omnigent-server`
image) — as a plain `Deployment`. The server runs "external-runner only": it
accepts runner connections at `/v1/runner/tunnel` and never executes agent
harnesses itself, so the pod needs **no special privileges** (an ordinary
container). Runners live elsewhere — a laptop, a managed sandbox
(Modal/Daytona), or the on-cluster `host/` stopgap.

Auth defaults to the built-in **`accounts`** provider (multi-user
username/password, first-boot admin, no external IdP), matching the deploy
default everywhere else. OIDC is an opt-in block in `server/deployment.yaml`.

`host/` is a **stopgap** for the missing piece: Omnigent has managed sandboxes
with a pluggable `sandbox.provider` (lakebox/Modal/Daytona) but **no
`kubernetes` provider** — that gap is `omnigent-ai/omnigent#39`. Until a
server-side Kubernetes sandbox provider lands, `host/` runs the `omnigent host`
daemon in a pod (booting the prebaked `omnigent-host` image) so a cluster node
becomes agent compute.

## TL;DR — bring it up

```bash
cd deploy/kubernetes
kubectl apply -f server/namespace.yaml
# Build the Secret (don't apply the example as-is — it's placeholders). See
# server/secret.example.yaml, or use sealed-secrets / SOPS / ESO:
kubectl -n omnigent create secret generic omnigent-secrets \
  --from-literal=database-url='postgresql://user:pass@host:5432/omnigent' \
  --from-literal=accounts-cookie-secret="$(openssl rand -hex 32)"
kubectl apply -f server/configmap.yaml server/pvc.yaml \
               server/deployment.yaml server/service.yaml \
               server/ingress.example.yaml       # adapt the Ingress to your controller
kubectl -n omnigent rollout status deploy/omnigent
# First admin: set accounts-admin-password in the Secret, or use the one-time web
# setup form on first visit (omnigent never auto-generates a password).
```

Server answers on the Service (`omnigent:8000`) and at your Ingress host
(`OMNIGENT_ACCOUNTS_BASE_URL`). Sign in as the admin you set via
`accounts-admin-password` (or created in the web setup form).

## Files

| | |
|---|---|
| `server/namespace.yaml` | The `omnigent` Namespace. Apply first. |
| `server/deployment.yaml` | The server `Deployment`. amd64 nodeSelector (image is linux/amd64-only), accounts auth env by default (OIDC block commented in-line), `DATABASE_URL` from the Secret, `/health` readiness+liveness probes, artifact PVC at `/data`, config ConfigMap at `/etc/omnigent`. The single source for which env vars the server reads — mirrors `deploy/docker/.env.example`. |
| `server/secret.example.yaml` | EXAMPLE Secret template (`omnigent-secrets`): `database-url` + `accounts-cookie-secret` (default), `oidc-client-secret` / `oidc-cookie-secret` (opt-in, commented). DO NOT commit real values — manage with sealed-secrets/SOPS/ESO/Vault. |
| `server/configmap.yaml` | Non-secret server config (`config.yaml`: `admins`, `allowed_domains`) — the same YAML `omnigent server -c` reads, loaded via `OMNIGENT_CONFIG`. `admins` promotes operators; `allowed_domains` is an **OIDC-only** email gate (accounts mode keys admins by username and doesn't enforce it). |
| `server/service.yaml` | ClusterIP `Service` → port 8000. |
| `server/ingress.example.yaml` | Generic `networking.k8s.io/v1` Ingress (host → `omnigent:8000`, TLS). Adapt to your controller (or use a controller CRD: Traefik IngressRoute, Contour HTTPProxy, …). Host must match `OMNIGENT_ACCOUNTS_BASE_URL` (and the OIDC redirect URI if you opt into OIDC). |
| `server/pvc.yaml` | `ReadWriteOnce` PVC for the artifact store at `/data`. Set your `storageClassName`. |
| `host/host.yaml` | The on-cluster `omnigent host` stopgap (#39): a Deployment + PVC running the prebaked `omnigent-host` image. One per node you want as compute (distinct `metadata.name` + `spec.hostname` + `nodeSelector`). |
| `host/secret.example.yaml` | EXAMPLE Secret (`omnigent-host`): the `omnigent login` `auth_tokens.json` (via `--from-file`, NOT inline — it must carry `expires_at`) + harness tokens (`CLAUDE_CODE_OAUTH_TOKEN` / `CODEX_ACCESS_TOKEN`). |
| `host/README.md` | The host stopgap walkthrough: prebaked image, token minting, and the security trust-boundary note. |

## Iterating on the deploy

```bash
# Re-apply after editing a manifest
kubectl apply -f server/deployment.yaml

# Roll the server (e.g. after a Secret/ConfigMap change it doesn't auto-watch)
kubectl -n omnigent rollout restart deploy/omnigent
kubectl -n omnigent rollout status  deploy/omnigent

# Tear down (PVC retains the artifact store + accounts DB unless you delete it)
kubectl delete -f server/ -f host/        # then: kubectl delete pvc -n omnigent --all
```

## Common debugging

| Symptom | Likely cause | First check |
|---|---|---|
| Pod `Pending`, never schedules | No amd64 node, or the PVC can't bind | `kubectl -n omnigent describe pod -l app=omnigent` — look for the nodeSelector or `FailedScheduling`/`unbound PersistentVolumeClaim` event; set a real `storageClassName` in `pvc.yaml`. |
| `CrashLoopBackOff` at startup, accounts error | Missing/short cookie secret or bad base URL | Logs show `OMNIGENT_ACCOUNTS_COOKIE_SECRET must be …` (needs 64 hex chars — `openssl rand -hex 32`) or `OMNIGENT_ACCOUNTS_BASE_URL must start with http:// or https://`. |
| `CrashLoopBackOff`, `psycopg.OperationalError` | `DATABASE_URL` wrong/unreachable | `kubectl -n omnigent get secret omnigent-secrets -o jsonpath='{.data.database-url}' | base64 -d` and verify the Postgres host/creds; first boot over a remote DB is slow (migrations). |
| No admin / can't sign in (accounts mode) | accounts mode never auto-generates a password | Set `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` (Secret key `accounts-admin-password`) for a headless admin, or open the URL and use the one-time web setup form (`/v1/info` reports `needs_setup`). |
| OIDC login bounces / "email present but email_verified is not true" | IdP asserts `email_verified: false` | Make the IdP emit `email_verified: true` (e.g. override Authentik's default `email` scope mapping). |
| Web UI loads but new chats hang forever | Expected — runners are external | Launch a runner (`omnigent run … --server <url>`) or bring up `host/`. |
| On-cluster host never connects | `auth_tokens.json` missing `expires_at` (treated as expired) | `--from-file` the real `~/.omnigent/auth_tokens.json`; don't hand-write a `{"token": …}`-only body. See `host/secret.example.yaml`. |

## Extending / the #39 gap

The right long-term fix is a **`sandbox.provider: kubernetes`** that the server
uses to spawn runner Pods on demand — the same launch-token seam the Modal and
Daytona providers already use — replacing the `host/` stopgap. Until then,
replicate `host/host.yaml` (Deployment + PVC, distinct `spec.hostname` +
`nodeSelector`) per node you want as compute.

## Related skills + docs

- [`deploy/README.md`](../README.md) — the deploy-options menu.
- [`deploy/docker/SKILL.md`](../docker/SKILL.md) — the shared image both the
  server and host targets are built from.
- `host/README.md` — the on-cluster host stopgap (security note included).
