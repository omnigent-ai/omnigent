# Omnigent on Kubernetes

Deploy Omnigent to any Kubernetes cluster using Kustomize. The manifests pull
the prebuilt image and set up a persistent volume and health checks. They also
include an Ingress so you can serve the app over HTTPS at a public web address,
but that part is optional — it only matters when people need to reach the server
over the internet, and it pulls in two extra add-ons (ingress-nginx and
cert-manager). For local or dev use, ignore it and connect with `kubectl
port-forward` (see [Verify the deployment](#verify-the-deployment)).

## What gets provisioned

- **Deployment** — single-replica pod running
  `ghcr.io/omnigent-ai/omnigent-server`, served on port 8000.
- **Service** — ClusterIP on port 80 → 8000.
- **Ingress** *(optional)* — serves the app over HTTPS at a public web address,
  using cert-manager for the certificate. Skip it if the server isn't going on
  the internet.
- **PVC** — 10 Gi volume at `/data/artifacts` for the artifact store, minted
  cookie secret, and admin credentials.
- **ConfigMap + Secret** — environment config and database credentials.

## Prerequisites

- A Kubernetes cluster (1.25+)
- `kubectl` with Kustomize support (`kubectl kustomize` or standalone `kustomize`)
- A PostgreSQL database (managed or in-cluster — see below)
- *Only if you're putting the server on a public web address:* an ingress
  controller (e.g. ingress-nginx) and cert-manager

### Install cluster add-ons for ingress and cert management (optional)

Skip this unless you're putting the server on a public web address. (For local
or dev use you'll reach it with `kubectl port-forward`, or you can let your own
load balancer or proxy handle HTTPS instead.) Otherwise, if your cluster doesn't
already have an ingress controller and cert-manager, install them (pin the
versions to taste):

```bash
# ingress-nginx — use the provider manifest that matches your cluster
# (this is the kind one; for EKS/GKE/AKS use that provider's manifest or Helm chart):
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# cert-manager:
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# wait until both are ready:
kubectl wait -n ingress-nginx --for=condition=Ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s
kubectl wait -n cert-manager --for=condition=Available deployment --all --timeout=180s
```

### Create a cert-manager issuer (optional)

Skip this unless you're using the Ingress. cert-manager fetches the HTTPS
certificate for the Ingress from a `ClusterIssuer` named `letsencrypt-prod`
(the `cert-manager.io/cluster-issuer` annotation in `base/ingress.yaml`). That
issuer is **not** shipped here — create one before deploying, or change the
annotation to match an issuer you already have. Two common choices:

```yaml
# Production — real certificates from Let's Encrypt
# (needs a public domain and an Ingress reachable from the internet):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
```

```yaml
# Local / dev — self-signed (no public DNS needed; browsers will warn):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  selfSigned: {}
```

Apply your chosen issuer with `kubectl apply -f <file>`. Without it, cert-manager
logs `IssuerNotFound` and no certificate is issued (the server still runs — only
TLS is affected).

## Deploy with an external database

Use this path when you have a managed Postgres (RDS, Cloud SQL, Neon, etc.).

1. **Edit the secret** — set your real `DATABASE_URL` and generate a cookie
   secret:

   ```bash
   # deploy/kubernetes/base/secret.yaml
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Set your domain** *(skip if you're not using the Ingress)* — replace
   `omnigent.example.com` in `base/ingress.yaml` with your domain, and make sure
   the `letsencrypt-prod` ClusterIssuer exists (see
   [Create a cert-manager issuer](#create-a-cert-manager-issuer)).

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/base/ | kubectl apply -f -
   ```

4. **Create the first admin.** Open the app (via your Ingress host, or
   port-forward for a quick check — see
   [Verify the deployment](#verify-the-deployment)). With the default `accounts`
   provider the first visitor claims the instance: the Setup screen prompts for
   a username + password, and whoever finishes it first becomes the admin.

## Deploy with in-cluster Postgres

The `overlays/postgres/` overlay adds a single-replica Postgres 16 StatefulSet
with its own 10 Gi PVC. Good for dev/testing clusters.

1. **Edit secrets** — in `overlays/postgres/secret-patch.yaml`, replace
   `changeme` with real passwords:

   ```bash
   POSTGRES_PASSWORD: "<strong-password>"
   DATABASE_URL: "postgresql+psycopg://omnigent:<strong-password>@postgres:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Set your domain** *(skip if you're not using the Ingress)* — edit the
   hostname in `base/ingress.yaml`, and make sure the `letsencrypt-prod`
   ClusterIssuer exists (see
   [Create a cert-manager issuer](#create-a-cert-manager-issuer)).

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/postgres/ | kubectl apply -f -
   ```

## Deploy on Red Hat OpenShift

The `overlays/openshift/` overlay replaces the Ingress with an OpenShift Route
(edge TLS, managed by the platform) and adds a `restricted-v2`-compatible
SecurityContext. No ingress controller or cert-manager add-ons needed.

1. **Edit the secret** in `base/secret.yaml` (same as the external-database
   path above).

2. **Set your route hostname** — replace `omnigent.apps.example.com` in
   `overlays/openshift/route.yaml` with your cluster's apps domain.

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/openshift/ | oc apply -f -
   ```

For in-cluster Postgres on OpenShift, use `overlays/openshift-postgres/`
instead — it combines the Postgres StatefulSet, OpenShift Route, and restricted
security contexts:

```bash
# edit overlays/openshift-postgres/secret-patch.yaml with real passwords first
kubectl kustomize deploy/kubernetes/overlays/openshift-postgres/ | oc apply -f -
```

## Verify the deployment

Check the rollout and reach the server without a public domain:

```bash
kubectl get pods -n omnigent          # omnigent (and, with the overlay, postgres) → Running
kubectl rollout status deploy/omnigent -n omnigent
kubectl logs -n omnigent deploy/omnigent          # server logs

# Port-forward the Service and open the app locally:
kubectl port-forward -n omnigent svc/omnigent 8000:80
# → http://localhost:8000   (health check: curl localhost:8000/health → {"status":"ok"})
```

The first boot runs database migrations before the server starts listening; the
pod may restart once if the liveness probe fires during that window (see
[Resource sizing](#resource-sizing)).

To test the Ingress itself instead of port-forwarding, point its hostname at a
domain that already resolves to localhost — `omnigent.localtest.me` or
`<node-ip>.sslip.io` — use the self-signed issuer above, and reach it through the
ingress controller's published port.

## Next steps: connect a host

The server is the control plane — agents run on **hosts** that register with it.
A brand-new deployment has none, so connect at least one machine:

```bash
omnigent login https://omnigent.example.com          # authenticate the CLI
omnigent host  --server https://omnigent.example.com # register this machine
```

The host then appears in the web UI when you start a new chat. See the
[main README](../../README.md) for the full host/auth reference.

## Use your own IdP instead (OIDC) — optional

Optional. The default `accounts` provider (username + password) works out of the
box; use this only to delegate authentication to an external OIDC provider. Add
OIDC env vars to the secret:

```bash
kubectl create secret generic omnigent-oidc -n omnigent \
  --from-literal=OMNIGENT_AUTH_PROVIDER=oidc \
  --from-literal=OMNIGENT_OIDC_ISSUER=https://github.com \
  --from-literal=OMNIGENT_OIDC_CLIENT_ID=<client-id> \
  --from-literal=OMNIGENT_OIDC_CLIENT_SECRET=<client-secret> \
  --from-literal=OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback \
  --from-literal=OMNIGENT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32)
```

Then add `envFrom: [{secretRef: {name: omnigent-oidc}}]` to the Deployment
container spec (or merge the values into `omnigent-secrets`).

## Resource sizing

The server idles around ~275 MB RSS. The manifests request 512 Mi and limit at
1 Gi — adjust to taste. The first boot against a remote Postgres runs
migrations and takes ~1 minute; bump the liveness `initialDelaySeconds` to
~90s if you see the pod get killed during the first deploy.

## Scaling

The server uses an in-memory runner registry, so **only one replica is
supported**. Do not increase `replicas` unless the architecture is changed to
use a shared registry (e.g. Redis).
