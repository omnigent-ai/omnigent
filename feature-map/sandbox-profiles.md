# Sandbox profiles

Administrators save named sandbox configurations in Settings. Users choose a
profile beside the repository picker when starting a managed session. Existing
sandboxes retain the configuration accepted at launch.

## Sub-features

- `catalog`: loading, empty, saved profiles, failed reads with retry, and pagination.
- `manage`: create, edit, disable, and re-enable profiles with stable IDs.
- `validation`: JSON object validation and provider configuration errors.
- `selection`: matching enabled profiles, deployment default, and changed-profile warnings.
- `revision`: session creation submits the selected profile ID and accepted revision.
- `gating`: administrator management and configured managed-provider availability.

## How to get to it (user POV)

- Desktop Settings: open Settings, then Admin → Sandboxes; use New profile,
  Edit, or Disable. Edit also contains the Enabled switch.
- Mobile Settings: open Settings, then Admin → Sandboxes in the settings
  navigation; the same actions open a scrollable editor.
- Direct link: `/settings/sandboxes` opens the catalog. On mobile, select
  Sandboxes from the initial settings navigation to reveal its content.
- Desktop new session: open New session or the home page, select a managed
  sandbox provider, and use the Sandbox profile menu beside the repository.
- Mobile new session: open New session, close the navigation drawer if open,
  and use the profile menu above the message composer.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). Profile
management needs an administrator and a configured supported managed provider.
The browser test below starts its own isolated server with SQLite, local admin
identity, and Kubernetes provider configuration; it does not need a runner or
cluster. Build this checkout with `pnpm --filter web run build` first.

- Desktop Settings, mobile Settings, direct link, and both new-session
  viewports are covered by
  `tests/e2e_ui/onboarding/test_sandbox_profiles.py::test_manage_and_select_sandbox_profile`.
  Run this own-environment test directly:

  ```sh
  OMNIGENT_SANDBOX_PROFILE_EVIDENCE_DIR=/tmp/sandbox-profile-proof \
    .venv/bin/python -m pytest tests/e2e_ui/onboarding/test_sandbox_profiles.py \
    --ui-skip-build --video=on --screenshot=on \
    --output=/tmp/sandbox-profile-proof/playwright
  ```

  It creates a profile, rejects a non-object JSON value, edits and reloads the
  saved configuration, selects the profile on desktop and mobile, verifies the
  submitted ID/revision, disables it, and re-enables it from mobile Settings.
  Catalog writes, reads, and API cross-checks use the real server. The session
  POST is intercepted and returns a controlled provisioning error; this proves
  the launch request and restored draft, not resource allocation or networking.
- Component checks cover management permissions, save errors, matching-provider
  selection, pagination, and Settings navigation while identity loads:

  ```sh
  pnpm --filter web test src/pages/settings/SettingsSandboxesSection.test.tsx \
    src/components/SandboxProfilePicker.test.tsx \
    src/shell/NewChatDialog.test.tsx src/shell/settingsNav.test.tsx \
    src/lib/sandboxProfilesApi.test.ts src/hooks/useIsAdmin.test.tsx
  ```

- For live allocation, start an isolated environment configured for a dedicated
  Kubernetes namespace or direct agent-sandbox provider. The server service
  account needs Pod and NetworkPolicy permissions, and the network plugin must
  enforce NetworkPolicy. Create a profile with known resource quantities, start
  a session, and inspect the allocated Pod resources. Verify any egress rules
  using known allowed and denied destinations. Repeat after sleep/wake and
  replacement to check the saved configuration. Record cluster evidence
  separately from the browser test; warm-pool launches do not support profiles.

## Gotchas

- The profile menu appears only when the chosen provider has an enabled profile
  or a profile is already selected. Default retains deployment configuration.
  Local-host sessions do not use this menu.
- A selected profile keeps its revision. If an administrator edits or disables
  it, a refreshed menu asks the user to choose again; the server rejects stale
  or unavailable selections before provisioning.
- Editing a profile affects new launches. Existing sandboxes keep their saved
  configuration; disabling a profile also prevents their subsequent wake or
  replacement until it is enabled again.
- JSON supports optional image, native Kubernetes resource requests/limits,
  and native NetworkPolicy egress rules. Omitting egress keeps deployment
  policy. Empty egress denies outbound traffic including DNS, the Omnigent
  server, and model gateways unless another policy allows it.
- Kubernetes allow policies combine. This editor does not provide read-only
  internet access or guarantee that private data cannot leave the sandbox.
- A fake successful session response would not prove provisioning. Keep the
  controlled failure visible in this browser test and disclose its boundary.
