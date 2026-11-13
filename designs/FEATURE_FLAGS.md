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
Unknown names, including unknown negative overrides, fail startup. Retired names
(`custom_agents_settings_ui`) are ignored with a startup warning, with or without `-`.
The former `OMNIGENT_HARNESS_INSTALL_ENABLED` switch is rejected with a
migration hint; use `OMNIGENT_FEATURES=harness_install` instead. The server resolves the set once at startup and publishes frontend-visible
values in `GET /v1/info` under `features`. Users must reload the web app after a
flag change because server capabilities are cached at page boot.

`omnigent/server/feature_flags.py` is the source of truth for known keys and
lifecycle metadata.

## Inventory

| Key | Default | Owner | Review by | Purpose |
| --- | --- | --- | --- | --- |
| `codex_plan_at_launch` | On | Server | 0.22.0 | Allows the new-session composer to start Codex in Plan mode. Missing or disabled support disables this launch option; connected hosts must also advertise support. |
| `session_orchestration` | On | Server | 0.22.0 | Private-owner peer messages and independent workers. Append `-session_orchestration` and restart every server replica to refuse new orchestration; already accepted work may finish. Existing reads and child operations remain available. Server-only; not exposed in `/v1/info`. |
| `sibling_sessions` | On | Server | 0.24.0 | Enables Add nested agent, same-sandbox fork selection, and new sibling creation through JSON, uploaded agents and MCP. Append `-sibling_sessions` to disable new nested creation. Existing sessions, family metadata and shared-sandbox cleanup protections remain available while off. |
| `automations_v2` | On | Server | 0.20.0 | Shared backend/UI rollout for automation improvements: scheduling, run management, execution targets and UI. Currently selects the DB scheduler instead of per-task timers and is exposed to the frontend. Requires a transactional store; a multi-replica deployment also supplies host ownership, and coordinated restart and startup draining are required when switching schedulers, including on the upgrade that turns it on. Append `-automations_v2` to keep the per-task timers, for example on Cloudflare D1. A server without a scheduled-task store runs no scheduler either way. See the [scheduler integration contract](../omnigent/server/scheduled/README.md). |
| `usage_page` | Off | Web | 0.11.0 | Exposes the web Usage route, sidebar navigation, timeline, and cost breakdown details. The existing `GET /v1/usage` CLI API remains available while off. |
| `harness_install` | Off | Onboarding | 0.11.0 | Allows the web UI to install or configure supported harnesses on a connected host. |
| `canvas` | Off | Web | 0.15.0 | Exposes the web Canvas route (`/canvas`) and its sidebar navigation: top-level sessions as draggable cards, one canvas per project. |
| `sidebar_grouping_ui` | On | Web | 0.23.0 | Adds Grouping, Ordering and Show options to the sidebar filter menu, moves that menu to the Projects header, splits Shared sessions into their own section, lets a Status or Updated grouping replace the Projects, Shared and Sessions sections, and expands the session tooltip with Repo, PR and Created by. Append `-sidebar_grouping_ui` to restore the previous sidebar until the flag is removed in 0.23.0. |
| `sidebar_windowed_rows` | Off | Web | 0.24.0 | Mounts only the sidebar session rows near the viewport, plus any row that is focused, has an open menu, or is being dragged, so long session lists stay responsive. Off keeps every loaded row mounted. |
| `session_overview` | Off | Web | 0.25.0 | On desktop, moves the session cost and per-model token usage from Agent info into a sidebar beside chat, toggled by a header cost chip. Wide windows with the workspace collapsed dock it open; narrower windows open it over chat. Mobile, and the flag off, keep cost in Agent info. |
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

Nested agents are enabled by default. Set `OMNIGENT_FEATURES=-sibling_sessions`
to disable them. Both `features.sibling_sessions` and the `sibling_sessions`
capability in `/v1/info` report the same value. Embedded deployments can pass a
`FeatureFlags` subclass to `create_app(feature_flags=...)` and override
`enabled(Feature.SIBLING_SESSIONS)` with a per-request rollout-service read.
That reader must default to false when configuration or request context is
unavailable. The capability and creation authorization both evaluate it per
request, including worker-thread context propagation. UI capabilities refresh
after a page reload; stale clients are still refused by the server.

Disabling nested agents stops new sibling admission, without cancelling accepted
work or removing existing families. Keep shared-sandbox retention enabled: those
protections apply to any sessions sharing compute, regardless of the flag.
MCP child creation and ordinary user-created roots remain available. MCP
`same_host` and `managed` placements create private standalone sessions while
the flag is off. Disable `session_orchestration` separately to stop independent
MCP creation and peer messages.

1. Deploy an immutable image with `-feature_name` in `OMNIGENT_FEATURES` to
   explicitly disable the feature, including one that defaults on.
2. Remove that disable and add `feature_name` on one deployment, consistently
   across all replicas.
3. Verify `GET /v1/info`, then reload and exercise the gated UI.
4. Expand by deployment cohort.
5. Roll back with `-feature_name` and redeploy the same image. For runner
   features, apply the override to the host/runner environment, restart those
   processes, and launch fresh terminals.

With `automations_v2`, the built-in server entrypoints run claimed runs with the
built-in executor and own every host, which suits a single replica. A
multi-replica deployment passes its own host ownership through `create_app()`.

For `session_orchestration`, verify the server gate with an actual peer-message
or independent-create request; `/v1/info` does not advertise this backend flag.
The server override is authoritative even when a host/runner enables the feature.
The tools remain advertised while disabled. This kill switch does not cancel
accepted work or disable existing session reads, ordinary messages or child work.
Ownership, private-source ancestry and runner-binding checks always apply.
Legacy sessions without the scheduled-origin marker remain eligible; the default
change does not backfill old scheduling history. See the
[orchestration feature map](../feature-map/session-orchestration.md).

Before enabling `automations_v2`, deploy the schema and updated code to every server replica. Older servers cannot read `starting` runs or preserve their cleanup records when deleting tasks.

Even with the flag off, an interrupted deletion can leave a hidden task row for
cleanup. During a mixed-version deployment, older replicas can still show and
edit that row until all replicas are upgraded.

Before disabling `automations_v2`, stop new dispatch and let existing startups
finish or complete recovery, including any required resource cleanup. Disabling
the flag stops startup recovery and deleted-task sweeps; clearing scheduling
cursors does not resolve unfinished runs or remove their retained task rows.
