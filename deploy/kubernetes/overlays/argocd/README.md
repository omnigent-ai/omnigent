# ArgoCD overlay

Deploy Omnigent with the kubernetes sandbox provider via ArgoCD. This overlay
adds safety annotations onto the
[`sandbox-runners`](../sandbox-runners/README.md) overlay:

- **`Prune=false`** on Namespaces and the artifact PVC, so an accidental prune
  or Application deletion does not cascade to operator-created Secrets and
  runner Pods.
- **Ingress in wave 1**, so its health check (which requires an ingress
  controller) does not gate the rest of the sync.

ArgoCD's built-in kind ordering already applies resources in dependency order
(Namespace → SA → Role → ConfigMap → Secret → Service → Deployment → Ingress),
so explicit sync-wave ordering for every resource is unnecessary.

ArgoCD renders Kustomize natively — no plugin or Helm chart needed.

## Quick start

1. **Fork the repo** — ArgoCD reads from Git, not your local disk. All edits
   below go into your fork and must be committed and pushed to the branch
   `targetRevision` names (default: `HEAD` / your default branch).

2. **Replace placeholder secrets** — `base/secret.yaml` ships `changeme`
   values. In your fork, set real values and commit:

   ```yaml
   # deploy/kubernetes/base/secret.yaml
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "<run: openssl rand -hex 32>"
   ```

   For production, manage `omnigent-secrets` externally (sealed-secrets or
   external-secrets) and remove `secret.yaml` from the overlay render with a
   `$patch: delete` — see `openshift/kustomization.yaml:12-20` for the pattern.
   The Application's `ignoreDifferences` entry prevents `selfHeal` from
   reverting out-of-band edits to this Secret's data.

3. **Configure server auth** — the default `accounts` provider refuses the
   managed runner dial-back (`403`). Front the server with **header or OIDC
   auth**, or run single-user. See
   [`sandbox-runners/README.md` § Server auth](../sandbox-runners/README.md#server-auth-managed-hosts).

4. **Set your domain** *(optional)* — replace `omnigent.example.com` in
   `base/ingress.yaml`. To skip the Ingress entirely, add a `$patch: delete`
   in your fork's overlay (see `openshift/kustomization.yaml:12-20` for the
   pattern — do not delete `base/ingress.yaml` itself, as it is shared by all
   overlays).

5. **Edit and apply the Application CR:**

   ```bash
   # In application.yaml, set repoURL to your fork and targetRevision to
   # the branch you pushed to:
   kubectl apply -f deploy/kubernetes/overlays/argocd/application.yaml
   ```

6. **Wait for the sync** — ArgoCD creates the namespaces asynchronously (up
   to 3 minutes without a webhook). Wait before creating the harness Secret:

   ```bash
   kubectl wait --for=jsonpath='{.status.phase}'=Active \
     namespace/omnigent-sandboxes --timeout=300s
   ```

7. **Create the harness-credentials Secret** — LLM API keys for runner Pods.
   Not in Git (credentials don't belong there):

   ```bash
   kubectl create secret generic omnigent-creds -n omnigent-sandboxes \
     --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
     --from-literal=OPENAI_API_KEY=sk-...
   ```

   For production, manage this with
   [sealed-secrets](https://github.com/bitnami-labs/sealed-secrets) or
   [external-secrets](https://external-secrets.io/).

## What ArgoCD does not create

ArgoCD does not *create* these resources — but it **owns the namespaces they
live in**. Deleting the Application (with the default finalizer) deletes both
namespaces and garbage-collects everything inside them, including:

- **`omnigent-creds` Secret** (step 7 above) — without it, runner Pods stall
  in `CreateContainerConfigError`. See the
  [sandbox-runners README](../sandbox-runners/README.md#apply) for which keys
  to set.
- **OIDC / external-auth Secrets** — if you front the server with OIDC, create
  the provider Secret separately (see the
  [base README](../../README.md#use-your-own-idp-instead-oidc--optional)).

The `Prune=false` annotations protect Namespaces and the PVC during **sync**
(accidental prune from a Git rename), but the Application finalizer bypasses
them on **deletion**. To make `kubectl delete application` orphan resources
instead of cascading, remove the `resources-finalizer.argocd.argoproj.io`
finalizer from `application.yaml`.

## What automated sync does

- **`prune: true`** — resources that leave Git are deleted from the cluster on
  the next sync. `Prune=false` annotations on Namespaces and the PVC exempt
  them.
- **`selfHeal: true`** — manual cluster edits are reverted to match Git.
  `ignoreDifferences` on `omnigent-secrets` and `omnigent-artifacts` exempts
  their data, so out-of-band credential edits and volume expansions are kept.
- **Deleting the Application** — with the finalizer, deletes both namespaces,
  the artifact PVC, and everything inside them. Without it, orphans everything.

## Customizing

Fork the repo, edit, commit, and push — ArgoCD picks up changes on the next
sync. Common adjustments:

- **Sandbox config** — `../sandbox-runners/sandbox-config.yaml` (namespace,
  image, node selector, resource limits, PVC mounts). Note: changes to
  ConfigMaps require a Pod restart to take effect (the server reads config at
  startup). Use `configMapGenerator` with a name-suffix hash to trigger an
  automatic rollout, or restart the Deployment manually after sync.
- **Server resources** — `../../base/deployment.yaml`.
- **Ingress** — `../../base/ingress.yaml` (hostname, TLS, annotations). To
  remove the Ingress, add a `$patch: delete` in the overlay (see
  `openshift/kustomization.yaml`).
- **In-cluster Postgres** — use `overlays/openshift-postgres/` as a reference
  for composing two overlays that share a base; adding `../postgres/` as a
  direct resource causes a duplicate-base error. Alternatively, apply the
  Postgres StatefulSet separately.

## ApplicationSet (multi-environment)

For staging/production splits, use an ArgoCD
[ApplicationSet](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/)
with a list generator. Point each entry at a different `targetRevision` (branch)
or fork the overlay directory per environment with its own config values.
