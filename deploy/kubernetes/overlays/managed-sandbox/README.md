# Kubernetes overlay: managed sandboxes

Deploys the Omnigent server configured to launch **server-managed sandbox hosts**
(Modal / Daytona / CoreWeave). The base K8s manifests deploy the server only —
they do not model sandbox config or provider credentials, and they default to
`accounts` auth, which managed dial-back can't use. This overlay closes those gaps.

```bash
kubectl apply -k deploy/kubernetes/overlays/managed-sandbox
```

## What it adds over `base`

| File | Purpose |
|------|---------|
| `sandbox-config.yaml` | ConfigMap mounted at `/etc/omnigent/config.yaml` carrying the non-secret `sandbox:` block (`OMNIGENT_CONFIG` points here). |
| `secret-patch.yaml` | Adds provider API creds + the LLM/git creds injected into sandboxes to `omnigent-secrets`. |
| `configmap-patch.yaml` | Sets `OMNIGENT_AUTH_ENABLED=0` and clears the inherited `OMNIGENT_AUTH_PROVIDER=accounts` so managed dial-back works in single-user mode (see Auth below). |
| `deployment-patch.yaml` | Mounts the config + sets `OMNIGENT_CONFIG`. |

## Before you apply — required edits

1. **`sandbox-config.yaml`** — set `provider`, set `server_url` to the **public** URL
   the provider's network can reach (not the in-cluster Service, not localhost; use
   your Ingress hostname or a tunnel), and the per-provider `image` / `env` list.
2. **`secret-patch.yaml`** — replace every `changeme`. Set the provider key matching
   your `provider`, plus the LLM/git creds whose NAMES appear in the config `env:` list.
   Prefer sealed-secrets / external-secrets over committing real values.
3. **Host image must ship `bwrap`** — the native harness terminals fail-loud without
   bubblewrap. The `host` Dockerfile target installs it; if you build your own host
   image, install `bubblewrap` there too.

## Auth (important)

Managed sandboxes open two connections back: the **host tunnel** (per-launch token,
always works) and a per-session **runner tunnel** (authenticates with a resolved
server credential, *not* the launch token, and the launch token has no user identity).
Under the default `accounts` auth the runner tunnel is **refused**. Two supported modes:

- **Single-user no-auth** (this overlay's default): `OMNIGENT_AUTH_ENABLED=0` and
  `OMNIGENT_AUTH_PROVIDER=""`. Simplest; appropriate for a trusted single-tenant
  deployment. No login.
- **Header proxy** (multi-user): set `OMNIGENT_AUTH_PROVIDER=header` and front the
  server with a reverse proxy / IdP that strips inbound `X-Forwarded-Email` and
  injects a trusted value on every request.
- **Native OIDC** (multi-user): set `OMNIGENT_AUTH_PROVIDER=oidc` plus the
  `OMNIGENT_OIDC_*` env vars and cookie secret. See `deploy/islo/README.md` and
  `deploy/cwsandbox/README.md`.

> Threading user identity into the per-launch token (so plain `accounts` auth could
> authorize the runner tunnel) is a future code change, not required for this deploy.

## Verify

```bash
kubectl kustomize deploy/kubernetes/overlays/managed-sandbox   # renders clean
# after apply:
kubectl -n omnigent get pod -l app=omnigent
kubectl -n omnigent exec deploy/omnigent -- sh -c 'cat /etc/omnigent/config.yaml'
# then: open the UI, create a session with host_type "managed", confirm the host
# registers and a command runs.
```
