# Release feature flags

Omnigent release features are deployment-wide, temporary rollout switches. They
are not authorization controls or user preferences.

## Configuration

Set the comma-separated `OMNIGENT_FEATURES` environment variable and restart or
redeploy the server:

```bash
OMNIGENT_FEATURES=usage_page,harness_install
```

Unset or empty uses the defaults below. A name enables a feature; `-name`
disables it. Explicit disables win regardless of order, so appending
`-claude_osc_status` is a kill switch even if `claude_osc_status` is also present.
Unknown names, including unknown negative overrides, fail startup.
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
| `canvas` | Off | Web | 0.15.0 | Exposes the web Canvas route (`/canvas`) and its sidebar navigation: top-level sessions as draggable cards, one canvas per project. |
| `custom_agents_settings_ui` | Off | Web | 0.18.0 | Exposes Custom agents settings when the server also advertises `agent_install`. |
| `sidebar_grouping_ui` | Off | Web | 0.19.0 | Adds Grouping, Ordering and Show options to the sidebar filter menu, moves that menu to the Projects header, splits Shared sessions into their own section, lets a Status or Updated grouping replace the Projects, Shared and Sessions sections, and expands the session tooltip with Repo, PR and Created by. |
| `claude_osc_status` | On | Runner | 0.20.0 | Uses Claude OSC 7501 reports for working status, with legacy file/pane fallback. Set on the host/runner; applies to newly launched Claude-native terminals after restart. Not exposed in `/v1/info`. |

At the review release, each flag must be removed by making the feature
unconditional, removing the feature, or moving a genuinely permanent operator
policy into normal server configuration.

Runner features use the same defaults and overrides, resolved once at resource-registry
startup. The host forwards `OMNIGENT_FEATURES` to its runners, including through
the CLI's background daemon. Changing a remote server's environment alone does
not change a separate host's runners. Restart the host and affected runners
after changing their feature set; existing terminals keep their launch
configuration. See the [native-harness feature map](../feature-map/native-harnesses.md#driving-it-with-the-repro-environment)
for Claude working-status acceptance and kill-switch verification.

## Rollout and rollback

1. Deploy an immutable image with `-feature_name` in `OMNIGENT_FEATURES` to
   explicitly disable the feature, including one that defaults on.
2. Remove that disable and add `feature_name` on one deployment, consistently
   across all replicas.
3. Verify `GET /v1/info`, then reload and exercise the gated UI.
4. Expand by deployment cohort.
5. Roll back with `-feature_name` and redeploy the same image. For runner
   features, apply the override to the host/runner environment, restart those
   processes, and launch fresh terminals.
