# Extension manifest

An extension registers a lightweight factory under the
`omnigent.extensions` Python entry-point group:

```toml
[project.entry-points."omnigent.extensions"]
review = "acme_review.plugin:get_manifest"
```

The factory returns immutable values from `omnigent.extensions` and should not
import runtime or UI implementation modules.

```python
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)


def get_manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id="acme.review",
        display_name="Acme Review",
        distribution="omnigent-acme-review",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset({ExtensionPermission.NAVIGATION}),
        pages=(
            PageContribution(
                id="acme.review.dashboard",
                title="Reviews",
                route="reviews",
                view="dashboard",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id="acme.review.primary-nav",
                label="Reviews",
                page="acme.review.dashboard",
                icon="search",
                order=500,
            ),
        ),
    )
```

## Rules

- IDs are lowercase, publisher-qualified, immutable, and globally collision
  checked. Contribution IDs begin with the owning extension ID.
- Page routes contain exactly one safe segment and are always namespaced.
- The manifest distribution/version must match installed package metadata.
- `requires_omnigent` is a PEP 440 release-line range.
- Browser paths are fixed to `dist/extension.js` and optional
  `dist/extension.css` inside the entry point's verified import package.
- Built-ins are reserved. All community extensions participating in a collision
  are disabled deterministically.
- One invalid field rejects the whole extension; one rejected extension does not
  prevent others from loading.

## Team contributions

Add an accountable `owner` for agent packs, policy presets, or server code:

```python
from omnigent.extensions import (
    AgentContribution,
    ExtensionDependency,
    ExtensionEntrypoints,
    PolicyContribution,
)

# Additional ExtensionManifest fields:
owner = "team-reliability"
dependencies = (ExtensionDependency("acme.shared", ">=1,<2"),)
entrypoints = ExtensionEntrypoints(server="acme_triage.hooks:activate")
agents = (
    AgentContribution(
        id="acme.triage.assistant",
        title="Incident triage",
        path="agents/triage",
        policies=("acme.triage.approvals",),
    ),
)
policies = (
    PolicyContribution(
        id="acme.triage.approvals",
        title="Approve OS tools",
        path="policies/approvals.yaml",
    ),
)
```

Paths are relative to the entry point's verified package, just like browser
assets. Include them in the distribution's package data. Agent directories use
the canonical `config.yaml` format and can contain bundled skills, tools, and
MCP configs. The portable spec's `name` follows normal agent-name rules; it is
independent of the namespaced contribution ID. Packs reject symlinks, paths
outside the package, more than 256 entries, over 32 MiB of source content, or
excessive directory depth.

Policy files use the existing default-policy mapping. Policy names start with
the extension ID; function policies can only reference registered handlers.
An agent may select presets from its own extension and its declared dependency
closure. Duplicate policy names fail rather than silently replacing rules.
Missing or invalid selected policies make the entire pack unavailable.

```yaml
acme.triage.approve-os-tools:
  type: function
  function: omnigent.policies.builtins.safety.ask_on_os_tools
```

Server entrypoints use `module:callable` syntax and must belong to the declaring
distribution. They are separate from the lightweight manifest factory:

```python
from omnigent.extensions import ExtensionContext, ExtensionHooks, SessionStatusNotification


async def activate(context: ExtensionContext) -> ExtensionHooks:
    async def observe(event: SessionStatusNotification) -> None:
        context.logger.info("Session %s became %s", event.session_id, event.status)

    async def shutdown() -> None:
        context.logger.info("Extension stopped")

    return ExtensionHooks(on_session_status=observe, shutdown=shutdown)
```

Declare `ExtensionPermission.EVENTS_SUBSCRIBE` to observe status notifications,
then explicitly grant it in operator configuration. `ExtensionContext` exposes
identity, a private copy of non-secret configuration, and an extension logger.
It provides no application stores, raw requests, or alternative approval path.
See [activation and trust](extensions.md) for failure and timeout behavior.

## Compatibility

The extension API major is independent of the Omnigent package version. V1
changes are additive; authors must ignore unknown catalog fields. Breaking
changes require a new extension API major. Deprecations name the Omnigent
release in which removal is planned and remain supported through the stated
window.

Extension packages can run the same manifest and bundle checks in their own
focused tests without starting a server:

```python
from pathlib import Path
from omnigent.extensions import check_extension_package

check_extension_package(
    get_manifest(),
    project_root=Path(__file__).parents[2],
    package_root=Path(__file__).parent,
)
```

`activation_events`, `when`, and command records are reserved metadata only in
V1. Do not rely on them until a later API explicitly documents execution.
