# Experimental repository-seeded warm pools

This prototype prepares Git workspaces in spare agent-sandbox Pods before a
session requests them. An operator defines an exact repository combination,
builds a seed image, and maintains a native warm pool for that profile. Sessions
with that combination can use the pool across different agents and harnesses.

The prototype is enabled only through its explicit CLI. It keeps the
`agent_sandbox` provider and existing credential store, owner-bound brokers,
managed-host identity, and PVC lifecycle. There is no production configuration
UI or automatic image/pool management. First configure the native controllers,
RBAC, networking, and generic fallback using the
[warm-pool runbook](../../deploy/kubernetes/overlays/sandbox-runners/warm-pool/README.md).

Install the Kubernetes extra explicitly in a development checkout:

```bash
uv sync --frozen --extra all --extra kubernetes --group dev
```

For a Vault-backed credential store, also pass `--extra vault`.

## Build a seed image

Supply a GitHub token through `OMNIGENT_WORKSPACE_SEED_GITHUB_TOKEN` in the build
process's environment. It must read every requested repository; the builder
requires a token even for public repositories. Run the builder with explicit
branches and a new output directory; repeat `--repo` for a combination:

```bash
.venv/bin/python -m omnigent.experimental.workspace_profiles.seed \
  --repo 'https://github.com/example-org/backend.git#main' \
  --output /path/to/private-build-context/workspace-seed
```

The output contains `manifest.json` and Git bundles. The command prints the
manifest's SHA-256 digest, computed from canonical JSON with sorted keys and
compact separators. Temporary clones and credentials are discarded. No builder
token, owner OAuth token, or managed-host token is added to the seed artifacts.

Build a host image from this prototype revision, with the experimental Python
modules installed in its Python environment. Copy the generated directory to
`/opt/omnigent-workspace-seed/`, owned by root and readable but not writable by
the sandbox user. Runtime commands use Python isolated mode, so source copied
into HOME or supplied only through `PYTHONPATH` is insufficient. Publish the
image and record its immutable `repository@sha256:...` reference.

**Private repository content is present in the image and spare Pods before user
assignment.** Use a private registry and trusted cluster nodes; restrict image
pulls and namespace exec access. Owner authorization gates host activation, but
does not encrypt the image or hide its contents from cluster operators.

## Define the operator catalog

`profiles.json` is a strict JSON array. Each entry contains exactly `name`,
`warm_pool`, `image`, and an inline `manifest`. Use versioned names and paste the
generated manifest unchanged. The following single-repository example has
illustrative commit and digest values that must be replaced:

```json
[
  {
    "name": "backend-main-v1",
    "warm_pool": "backend-main-v1",
    "image": "registry.example/omnigent-backend@sha256:3333333333333333333333333333333333333333333333333333333333333333",
    "manifest": {
      "version": 1,
      "repos": [
        {
          "url": "https://github.com/example-org/backend.git",
          "branch": "main",
          "directory": "backend",
          "commit": "1111111111111111111111111111111111111111",
          "bundle": "repo-0.bundle",
          "bundle_sha256": "2222222222222222222222222222222222222222222222222222222222222222"
        }
      ]
    }
  }
]
```

Manifest URLs are canonical HTTPS GitHub URLs. Branches are explicit; commit
IDs are 40 lowercase hex characters and SHA-256 values are 64. Directories and
bundle names must be safe single path components. Image tags alone are rejected.

Selection compares the **complete** canonical repository/branch/destination
set. Repository order does not matter when the derived destinations remain the
same. Destinations follow the existing workspace rules, including owner-based
disambiguation for repositories with the same basename. A subset, extra
repository, different branch, or different destination does not match. Such
requests use the normal generic launcher behavior. Keep that fallback's
configured image and pool free of repository seeds.

The UI's **Default** branch selection omits a branch. When the repository set
could match a profile, the server resolves omitted branches through the session
owner's GitHub connection, including Vault decryption and token refresh. It
selects a profile only if every resolved branch matches. Metadata failures or
missing connections use the generic fallback; branches are never guessed.
Explicit branches do not need this lookup. GitHub metadata has a 10-second
shared deadline and 5-second request deadlines; credential resolution inherits
the existing store/client timeouts. This lookup does not replace authorization
during activation.

Agent/harness selection is independent of the repository profile; the image
must still support the requested harness. Profiles use shared pool semantics,
so per-agent admission-time credential injection does not apply.

## Render and run

Use the same `server-config.yaml`, workspace-volume environment settings, and
prototype revision for manifest generation and the server. The ordinary server
config supplies namespace, ServiceAccount, resources, mounts, networking, and
the Pod-reachable `sandbox.server_url`. Its image and optional warm pool remain
the generic fallback; the catalog supplies each seed profile's image and pool.

```bash
.venv/bin/python -m omnigent.experimental.workspace_profiles.cli \
  --profiles profiles.json render \
  --config server-config.yaml --profile backend-main-v1 --replicas 1 \
  > backend-main-v1.yaml

kubectl --context YOUR_CONTEXT apply -f backend-main-v1.yaml

.venv/bin/python -m omnigent.experimental.workspace_profiles.cli \
  --profiles profiles.json serve -- \
  --host 127.0.0.1 --port 8000 -c server-config.yaml --no-open
```

The renderer emits a `SandboxTemplate` and `SandboxWarmPool`. Before becoming
Ready, each spare verifies its manifest and bundle digests and prepares its HOME
workspace. An empty pool can still create a cold member, so successful
allocation alone does not prove a warm hit.

After a claim is assigned, the server registers the owner-bound host. Activation
uses that host's GitHub broker credential to check every repository before
starting the host. On first activation it resolves each branch and fetches changes since the seed
commit, then continues normal host preparation. Builder credentials are never
used to authorize the session. GitHub and Databricks host setup continue through
their existing broker paths.

## Verify and understand the limits

1. Connect a test user's GitHub account with access to every profile repository.
   Record the Ready spare's Pod UID before requesting a session.
2. Create a managed session with exactly the catalog's repositories and matching
   branches. The UI's **Default** selection works when GitHub's current default
   matches the profile. Confirm its claim uses the profile pool and its allocated
   Pod UID was recorded before the request. Seeded sessions report
   **Preparing workspace**, while generic sessions report **Cloning repository**.
3. Inspect repository HEADs and create an uncommitted workspace marker. Suspend
   and wake the session; confirm its Sandbox/PVC and marker survive. Wake checks
   repository access again and preserves existing work without resetting it.
4. Try a different agent/harness with the same repository set, then an unmatched
   repository combination. Confirm profile selection and generic fallback,
   respectively. Normal model credentials and service access are still needed
   to test inference.

Run automated prototype checks with:

```bash
.venv/bin/python -m pytest tests/experimental -q
```

Retained allocations recover their original profile from UID-verified Sandbox
metadata. Wake binds omitted branches to that retained profile without looking
up current GitHub defaults, so a later default-branch change does not migrate or
reset existing work. Keep historical
profile definitions available: a missing definition or incompatible
manifest/image fails wake without deleting its workspace. Infrastructure
compatibility checks still apply.

Distinct catalog versions with the same repository/branch/destination set are
currently ambiguous for new allocation and are rejected. There is no separate
active/historical selector. Submodules and Git LFS are rejected. Only GitHub
repositories are supported, and fresh activation still
requires broker and GitHub connectivity. Access is based on the owner's GitHub
repository permissions; separate team scopes and per-profile infrastructure
settings are not implemented. This remains an operator-driven
prototype; it does not provide a production rollout or catalog-management UI.
