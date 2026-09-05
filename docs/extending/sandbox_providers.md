# Sandbox providers

Omnigent supports running agent hosts in remote sandboxes. Built-in providers
(Modal, Daytona, Blaxel, CoreWeave Sandbox, E2B, Islo, OpenShell, Boxlite,
Kubernetes)
ship with the core package. Third-party packages can add new providers through
the `omnigent.sandbox_providers` entrypoint group.

## How it works

Each sandbox provider implements the
[`SandboxLifecycle`](../../omnigent/onboarding/sandboxes/base.py) interface.
Providers that exec into a running sandbox (Modal, Daytona, …) inherit
`ExecModelHostLauncher`, which provides a default `start_host` that probes
`$HOME`, creates a workspace, clones a repo, and backgrounds `omnigent host`.
Providers whose sandbox boots running the host directly (Kubernetes) inherit
`SandboxHostLauncher` and override `start_host` to build the infrastructure
manifest instead.

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
