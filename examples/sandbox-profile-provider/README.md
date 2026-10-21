# JSON sandbox profile provider

This small community plugin adds `profile_demo`, a Kubernetes launcher whose
profiles and permissions live in a customer-owned JSON file. It reloads the file
for each list and selection. Users see public metadata; the selected config is
applied to the launcher after checking current access and the exact revision.
See the [public contract](../../docs/extending/provider_profiles.md).

## Install and run locally

From an Omnigent checkout containing the provider profile contract, on macOS or
Linux:

```bash
uv sync --group dev
uv pip install --python .venv/bin/python --no-deps -e examples/sandbox-profile-provider
uv run --no-sync python examples/sandbox-profile-provider/demo.py
uv run --no-sync python examples/sandbox-profile-provider/demo.py --user bob
uv run --no-sync python examples/sandbox-profile-provider/demo.py --workspace 2
```

Use `--no-sync` after installing the example so `uv` preserves the extra package.
Alice sees `demo-small` and `demo-large`; Bob sees only `demo-small`. Neither
has access in workspace 2. The first two commands that run the demo end with
`fake prepare/provision/resume passed`. The last reports no authorized profiles.
The fake lifecycle makes no Kubernetes calls and needs no token or IdP.
**Infrastructure is not tested by this demo or its unit tests.**

To reproduce a stale selection, copy the fixture and pause between listing and
selection:

```bash
PROFILE_DEMO_DIR=$(mktemp -d)
cp examples/sandbox-profile-provider/catalog.json "$PROFILE_DEMO_DIR/catalog.json"
uv run --no-sync python examples/sandbox-profile-provider/demo.py \
  --catalog "$PROFILE_DEMO_DIR/catalog.json" --pause-before-select
```

While paused, open the temporary JSON file in your editor and change
`demo-small.config.resources.requests.cpu` from `"1"` to `"2"`. Press Enter in
the demo: it rejects the old selection with `SandboxProfileChanged`. Run the
same command again without `--pause-before-select` to accept the new revision.
Repeat with the pause and remove `"alice"` from `demo-small.users`: selection
now fails with `SandboxProfileAccessDenied`. Editing `workspaces` or removing
the profile also revokes access. The checked-in fixture stays unchanged.

Run the checks with:

```bash
uv run --no-sync pytest examples/sandbox-profile-provider/tests -q --reruns 0
```

## Full local web demo

Install the example as above. Start the backend from a disposable directory so
the relative catalog path, database, artifacts, and user config are isolated.
Run this from the repository root in terminal 1:

```bash
PROFILE_REPO="$PWD"
PROFILE_WEB_DIR=$(mktemp -d)
cp examples/sandbox-profile-provider/catalog.json \
   examples/sandbox-profile-provider/server.yaml "$PROFILE_WEB_DIR/"
cd "$PROFILE_WEB_DIR"
OMNIGENT_CONFIG_HOME="$PROFILE_WEB_DIR/config" \
OMNIGENT_DATA_DIR="$PROFILE_WEB_DIR/data" \
OMNIGENT_AUTH_PROVIDER=header OMNIGENT_LOCAL_SINGLE_USER=0 \
OMNIGENT_AUTH_HEADER=X-Forwarded-Email OMNIGENT_AUTH_HEADER_STRIP_PREFIX= \
"$PROFILE_REPO/.venv/bin/python" -m omnigent.cli server \
  --host 127.0.0.1 --port 18767 --no-open \
  --config "$PROFILE_WEB_DIR/server.yaml" \
  --database-uri "sqlite:///$PROFILE_WEB_DIR/demo.db" \
  --artifact-location "$PROFILE_WEB_DIR/artifacts"
```

In terminal 2, from the repository root, start Vite with the existing proxy
header support. This identifies the local browser as Alice without an IdP:

```bash
pnpm install --frozen-lockfile --filter web
OMNIGENT_URL=http://127.0.0.1:18767 OMNIGENT_AUTH_TOKEN= \
OMNIGENT_PROXY_HEADERS='{"http://127.0.0.1:18767":{"X-Forwarded-Email":"alice"}}' \
pnpm --dir web dev --host 127.0.0.1 --port 15173 --strictPort
```

These trusted headers impersonate test users; keep both servers bound to
loopback. The supplied config makes Alice an administrator. Open
`http://127.0.0.1:15173/settings/sandboxes`: both profiles should be marked as
provider-managed and have no local editing controls. On the home page or New
session dialog, choose `profile_demo` and select a sandbox profile explicitly.
Check the same picker at a mobile viewport width. This verifies discovery and
selection without creating a sandbox. Starting a session invokes real Kubernetes
unless a test fixture replaces the lifecycle operations.

Restart Vite with `"bob"` in the header mapping and reload the page: Bob should
see only the small profile. Use `"charlie"` for the empty catalog and required
selection state. Inspect the actual API independently:

```bash
curl -fsS -H 'X-Forwarded-Email: alice' http://127.0.0.1:18767/v1/sandbox-profiles
curl -fsS -H 'X-Forwarded-Email: bob' http://127.0.0.1:18767/v1/sandbox-profiles
curl -fsS -H 'X-Forwarded-Email: charlie' http://127.0.0.1:18767/v1/sandbox-profiles
curl -i http://127.0.0.1:18767/v1/sandbox-profiles
```

Expect two, one, and zero rows, respectively; `managed_providers` still contains
`"profile_demo"` for Charlie. Managed rows have `config: {}` and no identity or
permission fields. The headerless request must return 401. Edit the disposable
catalog, then reload to observe changed visibility or a changed revision without
a backend restart. Stop both foreground processes with Ctrl-C. If the ports are
in use, choose new ones and update `server.yaml`, the proxy URL/header mapping,
and the browser/API URLs together.

**Workspace note:** the OSS web server runs in workspace **0**, while `demo.py`
defaults to workspace 1. The fixture explicitly allows both; workspace 2 is
denied. Production multi-tenant servers supply their own authenticated workspace
context. There is no client workspace override to add to these API requests.

## Installing only for an E2E subprocess

A fixture can install the real package into a temporary target without changing
the CI environment's installed packages:

```bash
PROFILE_PLUGIN_DIR=$(mktemp -d)
uv pip install --python .venv/bin/python --no-deps \
  --target "$PROFILE_PLUGIN_DIR" examples/sandbox-profile-provider
PYTHONPATH="$PROFILE_PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" \
uv run --no-sync python -c \
  'from omnigent.onboarding.sandboxes.registry import available_providers; assert "profile_demo" in available_providers()'
```

Pass that target on the **server subprocess's** `PYTHONPATH` before it starts,
alongside the checkout/SDK paths already supplied by its fixture. Both
`omnigent.community` and `omnigent.community.sandbox` extend their package paths
from `sys.path`, so no changes to core `omnigent.__path__` are needed. This uses
the package's actual built metadata and entry point; do not patch discovery or
handwrite `.dist-info`. Use a fresh subprocess, since registry state is cached.
For an own-environment browser test, supply `X-Forwarded-Email` in the browser
context, set `OMNIGENT_LOCAL_SINGLE_USER=0`, and allow workspace 0 in the catalog.
Keep profile methods real; replace only lifecycle calls if simulating a launch,
and label the resulting proof as a fake launch rather than a Kubernetes test.

## Catalog format

[`catalog.json`](catalog.json) contains two examples. Each entry has public
`id`, `name`, and optional `description`, plus private `users`, `workspaces`,
and `config`. Both the user and workspace must match; there are no implicit
wildcards. IDs in `users` must be the stable IDs supplied by the server's auth
provider, which are not necessarily email addresses.

The profile configuration requires `image` and optionally accepts `resources`
and native Kubernetes NetworkPolicy `egress`. Resource/egress validation
uses Omnigent's existing profile validator. Omitted egress keeps the deployment's
network behavior; `"egress": []` denies all egress, including DNS and traffic to
the Omnigent server. The fixture omits it to keep the initial example simple.

Namespace belongs to provider-level `CatalogConfig`, defaults to `omnigent-demo`,
and cannot be overridden by a profile. A fresh launcher must locate a sandbox
from stable provider config plus its ID for stop/delete/status, even after
profile access is revoked, the profile is deleted, or the catalog is unreadable.
Keep the provider namespace unchanged while any existing sandboxes reference it.

The revision is computed as SHA-256 over the entire config encoded with sorted
keys and compact JSON. Do not add a manually maintained `revision` field. A
settings edit invalidates prior selections, including saved sessions that wake
later. Formatting, display-text edits, and permission edits do not change the
config hash; permissions are always rechecked. Pin image digests for repeatable
real launches: a mutable `:latest` tag can change content without changing its
configured string. Cluster policy, credentials, and Kubernetes environment
defaults remain external to this declared config.

The catalog must be a local regular file, at most 64 KiB and 100 profiles.
Duplicate IDs, invalid settings, malformed JSON, and unreadable files fail
closed. Restrict file writes to the customer/operator and publish updates with
an atomic file replacement. Do not put credentials in the catalog.

## Using the same plugin with real Kubernetes

Install the example in the server's Python environment. Put an operator-owned
catalog on local storage and configure Omnigent's server:

```yaml
sandbox:
  provider: profile_demo
  server_url: https://omnigent.example.com
  profile_demo:
    catalog_file: /absolute/path/to/catalog.json
    namespace: omnigent-demo
```

In this checkout, install the Kubernetes Python client and reinstall the example:

```bash
uv sync --extra kubernetes --group dev
uv pip install --python .venv/bin/python --no-deps -e examples/sandbox-profile-provider
```

Create the provider's namespace and supply the standard launcher's kubeconfig
or in-cluster ServiceAccount. It needs the normal Kubernetes launcher permissions
for Jobs and Secrets, plus NetworkPolicies if using `egress`. See
[sandbox provider setup](../../docs/extending/sandbox_providers.md).

Replace the fixture user/workspace IDs with identities from your authenticated
server, restart it to discover the installed plugin, and open the sandbox
profile picker. Alice's equivalent should see two choices and Bob's one.
Select a profile explicitly and create a managed session. This provisions real
infrastructure; inspect the resulting Job's namespace, image, and resources.
Revoke access in the file and verify a later launch or wake fails. A config edit
invalidates the saved revision, so an old session cannot silently wake using
new settings. Terminate test sandboxes through Omnigent when finished.

The plugin inherits Kubernetes lifecycle operations unchanged. The fake demo
does not validate any of these real infrastructure steps.
