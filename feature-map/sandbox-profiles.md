# Sandbox profiles

Customers own sandbox configuration and authorization in their provider plugin.
Users choose an approved profile when starting or cloning a managed session.
Omnigent stores the provider/profile revision and trusted execution identity.

## Sub-features

- `catalog`: per-user discovery, loading, empty results, server search, a 20-result cap and retry.
- `selection`: explicit selection for participating providers; exact revisions.
- `authorization`: provider checks on create, clone, reuse, wake and replacement.
- `gating`: default-off feature; ordinary providers retain deployment defaults.

## How to get to it (user POV)

- Desktop home page or New session dialog: select a sandbox provider, then use
  the Sandbox profile menu beside the repository picker.
- Mobile new session: close the navigation drawer if open and use the profile
  menu above the composer.
- Clone on desktop/mobile: session actions → Clone session → sandbox destination
  → Sandbox profile. A same-provider fork defaults to the source profile and reports
  a newer offered revision. Starting/cloning requires an authorized choice.
- Message-action branch clones, sidebar clone actions and the error-card fork
  action use this same profile-enabled fork dialog.
- Reconnect → Clone uses the same searchable, capped picker and source-profile
  default when a sandbox destination supports profiles.
- Settings has no sandbox editor. `/settings/sandboxes` falls back to General.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). Enable
`OMNIGENT_FEATURES=sandbox_profiles` and configure a provider with
`manages_profiles = True`. The example uses a customer-owned local catalog.
Build this checkout with `pnpm --filter web run build` first.

`tests/e2e_ui/onboarding/test_sandbox_profiles.py::test_provider_owned_profiles`
is an own-environment test. It installs the actual example package in a temporary
directory and runs an isolated authenticated server with SQLite. It covers
per-user desktop/mobile choices, clone, denied/omitted selection, revocation,
provider outage and absent Settings. It needs no cluster or runner.

```sh
OMNIGENT_SANDBOX_PROFILE_EVIDENCE_DIR=/tmp/sandbox-profile-proof \
  .venv/bin/python -m pytest tests/e2e_ui/onboarding/test_sandbox_profiles.py \
  --ui-skip-build --video=on --screenshot=on --reruns 0 \
  --output=/tmp/sandbox-profile-proof/playwright
```

Allowed browser create/fork POSTs return an intercepted provisioning error;
this proves request contents and preserved drafts, not infrastructure allocation.
Direct API denials use the real server. Backend lifecycle coverage uses a
recording launcher in `tests/server/integration/test_sandbox_profile_launch.py`.

`tests/e2e_ui/onboarding/test_sandbox_profiles.py::test_default_launch_without_profile_feature`
checks no profile queries, no picker and unchanged launch payloads with the flag
off or older-server feature metadata. Enabled-provider opt-out is covered by
`web/src/shell/NewChatDialog.test.tsx::uses only selections advertised by the server and provider`,
including the unchanged launch payload and absent picker after discovery.

Component checks cover the new-session dialog/home composer, attachments, clone
and branch clone, scoped choices, stale drafts, retry and pagination:

```sh
pnpm --dir web exec vitest run src/components/SandboxProfilePicker.test.tsx \
  src/lib/sandboxProfilesApi.test.ts src/lib/sessionsApi.test.ts \
  src/shell/NewChatDialog.test.tsx src/shell/NewChatDialog.projectPrefill.test.tsx \
  src/shell/ForkSessionDialog.test.tsx
```

For actual allocation, follow the [example guide](../examples/sandbox-profile-provider/README.md#using-the-same-plugin-with-real-kubernetes)
against a dedicated namespace. Inspect image/resources, repeat wake/replacement,
revoke access, and terminate test sandboxes. Record cluster and directory
integration evidence separately; the browser test does not exercise them.

## Gotchas

- Empty catalogs for participating providers block launch. There is no Default
  fallback in the picker. Nonparticipating providers have no profile picker.
- `sandbox.default_profile_id` gives a participating provider a server-side default
  for creates that name no profile (API clients, scheduled fresh sandboxes). It is
  authorized and saved like an explicit pick. `GET /v1/sandbox-profiles` reports it as
  `default_profile_ids`, and the composer and fork dialog preselect it when nothing else
  is chosen.
- The composer starts on the profile last picked on that provider (saved on pick, per
  provider in `localStorage`), resolved by id on its current revision, ahead of the
  default. A profile the provider no longer lists falls back to the default.
- Discovery is scoped to the selected provider so unrelated outages do not block
  ordinary launches. Listing is advisory; the server reauthorizes selection.
- A stale or revoked choice requires explicit reselection. Saved sessions cannot
  silently switch revisions. A provider may retain old authorized revisions.
- Forks authorize the new caller; wake authorizes the saved execution identity.
  Revocation blocks subsequent use but does not terminate an existing sandbox.
- Cleanup uses stable provider configuration and sandbox IDs, independent of
  profile availability. Resource location must survive catalog changes.
- The example owns image/resource configuration. Core has no configuration schema,
  admin editor, catalog writes, directory connector or identity management.
- Scheduled fresh sandboxes reject profile-requiring defaults at task create/update
  unless the default provider has a default profile, which each fire then saves.
  The session-create tool accepts explicit provider/profile/revision fields for managed placement.
  Native `/clear` and `/fork` reauthorize and
  bind the replacement before detaching the source; the last directly bound
  runner session cannot detach. Host and profile mismatches remain blocked.
  claude-sdk `/clear` continues in an empty fork (authorized like `/fork`) bound
  to the source's host, workspace and live runner. It stays in the same sandbox,
  so it keeps the source's `omnigent.sandbox.*` labels: a relaunch re-clones
  the same repositories and routes by the same host id. No history or files
  are copied, and a fork that cannot be bound is deleted again.
  claude-sdk and Claude Code native `/rewind` continue the same way, in a fork
  holding the history before the chosen prompt and the source's files. Both
  reauthorize the source's accepted profile on the continuation, so with no
  live runner it still wakes the sandbox after the source is deleted; a
  profile revoked since it was accepted refuses the command with a 403 and
  leaves no continuation
  (`tests/server/integration/test_sandbox_profile_launch.py::test_offline_clear_continuation_keeps_the_accepted_profile`).
- Missing sandboxes use the bound session's reference for replacement. Legacy host
  recovery rejects histories over 200 sessions; use controller lookup for larger
  histories. Runner-host discovery and reconnect liveness have a separate limit
  of 10,000 attached sessions; launch a new runner on overflow. Rotation, adoption
  and rollback use a smaller source lookup of at most 201 candidates and preserve
  the binding if no source is found. Ordinary hosts skip recovery scans.
  Native `/clear` retains old sessions on the host, so a long-lived sandbox without
  controller lookup can reach the host limit through normal use. Ordinary-only
  deployments skip runner profile discovery; mixed deployments keep its safety bound.
- Rollout is opt-in. Drain profiled workloads before disabling the flag or rolling
  back to a server without profile authorization; retain surviving session labels and controller sandbox assignments. No schema
  changes or new indexes are involved.
  Before first opt-in, complete the [legacy-label audit](../docs/extending/provider_profiles.md#rollout-rollback-and-verification)
  after upgrading every writer. Enable the flag before configuring a provider that requires profiles.
- Existing unprofiled sessions retain wake, relaunch, missing-sandbox replacement
  and runner rotation after opt-in. The provider must retain its unselected
  lifecycle path; scoped lookup returns `None` only for an authorized legacy
  resource. Fresh sandbox creation still requires an explicit selection or a
  configured provider default, and saved profiles never fall back to defaults. The rollout regression lives in
  `tests/server/integration/test_sandbox_profile_launch.py::test_unprofiled_session_survives_provider_opt_in`.

Controller recovery is covered in `tests/server/integration/test_sandbox_profile_controller.py`: last-session deletion, restart, historical revision, revocation and missing-sandbox replacement. The [controller walkthrough](../examples/sandbox-profile-provider/README.md#controller-recovery-demo) is runnable without a cluster.
