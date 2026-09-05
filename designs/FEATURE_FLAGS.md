# Release feature flags

Omnigent release features are deployment-wide, temporary rollout switches. They
are not authorization controls or user preferences.

## Configuration

Set the comma-separated `OMNIGENT_FEATURES` environment variable and restart or
redeploy the server:

```bash
OMNIGENT_FEATURES=usage_page,harness_install
```

Unset or empty means every release feature is off. Unknown names fail startup.
The former `OMNIGENT_HARNESS_INSTALL_ENABLED` switch is rejected with a
migration hint; use `OMNIGENT_FEATURES=harness_install` instead. The server resolves the set once at startup and publishes frontend-visible
values in `GET /v1/info` under `features`. Users must reload the web app after a
flag change because server capabilities are cached at page boot.

`omnigent/server/feature_flags.py` is the source of truth for known keys and
lifecycle metadata.

## Inventory

| Key | Default | Owner | Review by | Purpose |
| --- | --- | --- | --- | --- |
| `usage_page` | Off | Web | 0.11.0 | Exposes the web Usage route, sidebar navigation, timeline, and cost breakdown details. The existing `GET /v1/usage` CLI API remains available while off. |
| `harness_install` | Off | Onboarding | 0.11.0 | Allows the web UI to install or configure supported harnesses on a connected host. |

At the review release, each flag must be removed by making the feature
unconditional, removing the feature, or moving a genuinely permanent operator
policy into normal server configuration.

## Rollout and rollback

1. Deploy an immutable image with the feature absent from `OMNIGENT_FEATURES`.
2. Enable it on one deployment, consistently across all replicas.
3. Verify `GET /v1/info`, then reload and exercise the gated UI.
4. Expand by deployment cohort.
5. Roll back by removing the key and redeploying the same image.
