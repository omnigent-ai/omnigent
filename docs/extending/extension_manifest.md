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
