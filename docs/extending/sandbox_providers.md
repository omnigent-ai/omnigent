# Sandbox providers

Omnigent supports running agent hosts in remote sandboxes. Built-in providers
(Modal, Daytona, Blaxel, CoreWeave Sandbox, E2B, Gensee, Islo, OpenShell,
Boxlite, Kubernetes, microsandbox)
ship with the core package. Third-party packages can add new providers through
the `omnigent.sandbox_providers` entrypoint group.

## Harness providers and model lists

Managed Kubernetes and Agent Sandbox deployments can bind individual harnesses
to different inference gateways and curate the models shown in both composers.
See [Model and provider selection](../../designs/MANAGED_SANDBOX_MODEL_SELECTION.md)
for configuration, credential setup, session lifetime, and verification steps.

## How it works

Each sandbox provider implements the
[`SandboxLifecycle`](../../omnigent/onboarding/sandboxes/base.py) interface.
Providers that exec into a running sandbox (Modal, Daytona, …) inherit
`ExecModelHostLauncher`, which provides a default `start_host` that probes
`$HOME`, creates a workspace, clones a repo, and backgrounds `omnigent host`.
Providers whose sandbox boots running the host directly (Kubernetes), or whose
control plane owns host startup (Gensee), inherit `SandboxHostLauncher` and
override `start_host` without exposing a general-purpose exec transport.

## Faster initial Git clones

Admins can reduce the history downloaded for new managed sandbox checkouts:

```yaml
sandbox:
  provider: agent_sandbox
  server_url: https://omnigent.example.com
  git_clone:
    depth: 50
    single_branch: true
```

This is a useful starting point for short coding sessions. It checks out all
files on the selected branch, with up to 50 commits of history. It applies to
each requested repository, including those cloned after claiming a warm pool
Pod. No prepared workspace image is required. Restart the server after changing
its configuration and create a new sandbox to try it.

| Setting | Default | Behavior |
| --- | --- | --- |
| `depth` | `null` | Full history; a positive integer requests shallow history. |
| `single_branch` | `null` | Preserve the existing rule: an explicit repo branch selects one branch; otherwise fetch all branches. `true` selects only the requested/default branch; `false` fetches all branches. |
| `filter` | `null` | `blob:none` enables partial cloning: keep commit history and fetch file contents as needed. The initial checkout still includes all files. Requires remote filter support; Git can warn and fall back to an unfiltered clone. |
| `tags` | `true` | Normal Git tag following. `false` uses `--no-tags`, which persists for later fetches. `true` does not promise to fetch every remote tag. |

Omitting `git_clone` preserves existing commands and behavior. Setting only
`depth` preserves the existing branch selection, explicitly countering Git's
implicit single-branch default for shallow clones. Fetching every branch of a
repository with many branch tips can erase most of the speed benefit; configure
`single_branch: true` when that tradeoff fits the workload.

Settings apply only when a checkout needs to be created. Retained checkouts are
not reset, truncated, or reconfigured when a session wakes. An agent-sandbox
wake that lost its ephemeral workspace recreates it using the current policy.
Each entry in `sandbox.providers` can override the entire shared `git_clone`
mapping; use `git_clone: {}` on an entry to restore compatibility defaults.

Kubernetes, Agent Sandbox (including warm pools), and providers using the
standard exec clone implementation support these options. Gensee, Islo, and
custom workspace materializers must explicitly implement support; nondefault
settings otherwise fail before sandbox provisioning, replacement, or resume.
Third-party providers advertise `SandboxCapabilities.git_clone_options=True`
and honor `RepoWorkspace.git_clone`. Exec providers that override
`materialize_workspace` must accept its optional `git_clone` argument when
opting in. With default settings, legacy overrides receive no new keyword.

Clone settings do not contain credentials. Kubernetes and Agent Sandbox keep
using the owner-bound GitHub broker for initial clones and the host's existing
credential helper for later Git operations. Private-repo deepening and lazy
blob fetches require continuing access to the remote and valid credentials.
Vault and Databricks connector configuration is independent of this policy.

### Getting more history later

Inside a shallow checkout:

```bash
git fetch --deepen=100 origin
# Or restore full history for the configured branch selection:
git fetch --unshallow origin
```

Neither command widens a single-branch fetch refspec or re-enables tags. To fetch
another branch for a PR comparison, name its remote-tracking destination:

```bash
git fetch origin '+refs/heads/main:refs/remotes/origin/main'
# If both branches need more history, include both refspecs:
git fetch --deepen=100 origin \
  '+refs/heads/main:refs/remotes/origin/main' \
  '+refs/heads/feature:refs/remotes/origin/feature'
git fetch --tags origin  # explicitly fetch tags if needed
```

Replace `main` and `feature` with the relevant branches. When local PR diffs
cannot find a merge base because history is shallow, Omnigent reports an
actionable error rather than showing a misleading branch-tip comparison.
Unfetched base branches and failed lazy file retrieval also produce errors;
unavailable content is not treated as an added or deleted file.

Keep full-history defaults for history-heavy development, offline work, or tools
that expect broad history. `blob:none` is opt-in because it moves work to later
reads: a first historical `git show` or `git blame` can fetch more data and take
longer. Sparse checkout, submodule recursion, and LFS behavior are unchanged.

## Creating a community sandbox provider

### 1. Implement the launcher

```python
# omnigent_community_sandbox_acme/launcher.py
from omnigent.onboarding.sandboxes.base import ExecModelHostLauncher
from omnigent.onboarding.sandboxes.types import SandboxCapabilities


class AcmeSandboxLauncher(ExecModelHostLauncher):
    provider = "acme"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=False,
            foreground_exec=True,
        )

    def prepare(self) -> None:
        # Verify credentials / tooling
        ...

    def provision(self, name: str) -> str:
        # Create the sandbox, return its id
        ...

    def run(self, sandbox_id: str, command: str, *, check: bool = True):
        # Execute a command in the sandbox
        ...

    def put(self, sandbox_id: str, local_path, remote_path: str) -> None:
        # Copy a file into the sandbox
        ...

    def terminate(self, sandbox_id: str) -> None:
        # Delete the sandbox
        ...
```

### 2. Register the contribution

```python
# omnigent_community_sandbox_acme/plugin.py
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)

def get_contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="omnigent-acme",
        providers={
            "acme": SandboxProviderMetadata(
                name="acme",
                launcher_class="omnigent.community.sandbox.acme:AcmeSandboxLauncher",
            )
        },
    )
```

### 3. Declare the entrypoint in `pyproject.toml`

```toml
[project.entry-points."omnigent.sandbox_providers"]
acme = "omnigent_community_sandbox_acme.plugin:get_contribution"
```

### 4. Install and use

```bash
pip install omnigent-community-sandbox-acme
omnigent sandbox create --provider acme --server https://your-host
```

## Namespace requirement

Community provider code **must** live under the `omnigent.community.sandbox`
namespace package. This is enforced by the registry's validation — a
contribution whose `launcher_class` points outside this namespace is rejected
with a clear error.

To use the namespace, create a package under `omnigent/community/sandbox/` in
your distribution:

```
omnigent/
  community/
    sandbox/
      acme/
        __init__.py
        launcher.py
```

The `omnigent.community.sandbox` namespace package is already set up by core
Omnigent using `pkgutil.extend_path`, so your package's files are discovered
automatically when installed.

## Server-managed sandboxes

To use a community provider for server-managed sessions, add it to the
server's `sandbox:` config:

```yaml
sandbox:
  provider: acme
  server_url: https://your-host
  reaper:
    enabled: true
    terminate_after_offline_days: 30  # optional; defaults to 30 days
    sweep_interval_s: 86400           # optional; defaults to 1 day
```

The server resolves the provider through the registry and calls
`prepare()` → `provision()` → `start_host()` → wait for online registration.
Each managed sandbox authenticates back with a server-minted per-launch token.

Sandbox automations use this same launch path and create a fresh sandbox for
each run. Existing managed sandbox hosts cannot be pinned as automation targets.
Their lifecycle follows the server's sandbox configuration, just like ordinary
chats: automations do not override timeouts or terminate a sandbox when a run
finishes. Server owners must choose provider lifetime, idle, and cleanup settings
appropriate for their automation frequency and resource budget.

For `agent_sandbox`, `sandbox.keep_warm_s` controls the runner idle timeout;
the existing keepalive renews the sandbox deadline while the runner is alive.
Once renewal stops, expiry suspends compute while retaining storage. Other
providers have different lifetime behavior, so enabling managed sandboxes alone
does not guarantee a short shutdown time.

`sandbox.reaper` is deployment-wide: configure it next to `provider` or
`providers`, never inside one provider entry. One configurable loop covers every
configured provider and dispatches termination through the provider recorded on
each managed host. Reaping keeps the session and durable host binding so the
next message can launch a fresh sandbox generation.

The loop discovers workspaces with active or pending managed sandboxes, then
queries stale managed hosts within each workspace. Before calling the provider,
it atomically detaches the stale sandbox id from the active host generation.
Failed terminations stay pending and are retried by later sweeps; providers must
therefore make `terminate()` succeed when the sandbox is already absent.

The reaper reuses one launcher per workspace and provider. Override
`reaper_identity(workspace_id)` when background termination needs a scoped
credential context; the default context is a no-op.

## Provider capabilities

Providers declare their feature set via a `capabilities` property returning
`SandboxCapabilities`:

| Capability | Description |
|---|---|
| `cli_bootstrap` | Supports `omnigent sandbox create` / `connect` |
| `managed_launch` | Supports server-managed `host_type="managed"` sessions |
| `local_port_forward` | Can bridge a local port into the sandbox |
| `resume_stopped` | Can resume a stopped sandbox in place |
| `programmatic_terminate` | Can terminate a sandbox programmatically |
| `file_copy` | Supports copying files into the sandbox |
| `streaming_exec` | Supports streaming process execution |
| `foreground_exec` | Supports a foreground exec with inherited stdio |

## Network policy for harness bridge servers

Native harnesses (e.g. `claude-native`) run small HTTP servers on the sandbox
host that the harness's hook and helper subprocesses call back into: a tool
relay, which **by default binds loopback only (`127.0.0.1`)** so an ordinary
host keeps it off every other interface, and an MCP control ingress, which by
default listens on a Unix domain socket under the harness socket root
(`/tmp/omnigent-<uid>/mcp-<pid>.sock`) and so needs no network policy at all.

A sandbox whose SSRF hardening denies loopback destinations unconditionally
(e.g. OpenShell) cannot reach a loopback-advertised relay, which fail-closes
every prompt. Such an integrator opts into an all-interfaces bind by setting
`OMNIGENT_BRIDGE_BIND_HOST=0.0.0.0`: the servers then listen on all interfaces
(loopback consumers keep working) and advertise the host's routable address so
the in-sandbox hooks can reach them. Their ports come from a small stable pool
a network policy can allowlist by exact host+port:

- **`OMNIGENT_BRIDGE_BIND_HOST`** selects the posture. Unset (default) is
  loopback-only for the relay and a Unix socket for the MCP ingress. `0.0.0.0`
  binds both servers on all interfaces and advertises the detected routable
  address (falling back to loopback when the host has none). Any other value
  pins that exact host for both bind and advertisement.
- **Default port pool:** `28700`–`28715`
  (`omnigent.harnesses.claude_native.bridge.DEFAULT_BRIDGE_PORT_POOL`).
  Several servers coexist per host (the MCP ingress plus one tool relay per
  session), so allowlist the whole pool, not a single port. When the pool is
  exhausted the servers fall back to an OS-assigned ephemeral port, which an
  exact-port policy cannot cover.
- **`OMNIGENT_BRIDGE_PORT_POOL`** overrides the pool: comma-separated ports
  and inclusive ranges, e.g. `28700-28703,29000`.

Every endpoint on these servers (other than `GET /health`) requires a
per-relay bearer token, so allowlisting their coordinates does not expose
unauthenticated functionality.
