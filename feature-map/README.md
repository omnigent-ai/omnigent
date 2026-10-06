# Omnigent feature map

This directory is the maintained source for verifying Omnigent's user-facing
behavior. Read this index before driving the app, then use the matching feature
file as the recipe. Humans, reproduction and resolution agents, and triage all
read the same files.

The map describes features from the user's point of view: what the feature is,
every way a user reaches it, how to drive it, and the traps that invalidate a
run. It deliberately omits implementation details such as selectors, test IDs,
CSS classes, and source paths. Discover those at run time from the code and
the referenced tests; they change faster than the user journey.

## Skills

Read the relevant skill file directly when its capability is needed:

- [Verify Omnigent](skills/verify-omnigent/SKILL.md): start an isolated test
  environment, drive user journeys, and capture evidence. Its executable helper
  is `feature-map/skills/verify-omnigent/scripts/verify-env`.

These skills live here with the map so the whole package can be included or
excluded in replay. Harnesses do not automatically discover this directory:
start from this README, then read the linked SKILL.md before using its helper.
No slash-command registration or symlink is required for agents to read it.
Feature recipes also link existing repository skills outside this package;
check that they exist at the checkout revision before using them.

## Baseline preconditions

- Launch the isolated environment from the [skill](skills/verify-omnigent/SKILL.md#launch). It
  starts a server, runner, and mock model server on private ports with its own
  config, data, Claude, and Codex directories.
- Run drives that reuse this instance through `verify-env run -- ...` (or, in CI,
  `python -m dev.repro_env exec -- ...`) so the process receives the
  environment's server, model, and runner URLs. Tests marked "own environment"
  and component tests use the separate commands in each feature file.
- Never drive an instance this run did not start. A host daemon, a developer
  server, or another agent's environment is out of bounds.

## Driving conventions

- Prefer an existing test over hand-driving. Each feature file names the tests
  that exercise its entry points. For E2E UI tests, use `--ui-skip-build` after
  building this checkout, recording enabled, and an output directory under the
  evidence location. Server and component tests do not use browser flags.
- Hand-drive only the entry points no test covers, using Playwright against
  `OMNIGENT_REPRO_SERVER_URL`. Use roles and accessible names first.
- Script model replies with the mock helpers in `tests/e2e_ui/conftest.py`
  (`configure_mock_llm`, `set_fallback_mock_llm`, `reset_mock_llm`) before
  each journey. Journeys share mock state, so run them one at a time.
- Start each recipe from a fresh session unless its preconditions say otherwise.

## Proof and coverage

- Capture the user action and the resulting state, not only the final screen.
- UI proof is a recording or screenshot that shows the discriminating state,
  plus a read-only cross-check through the product REST API when one exists.
- Terminal proof includes the transcript; CLI proof includes the command,
  output, and exit code.
- Record the feature file and entry point with every artifact.
- **A fix is verified only when every entry point listed for the feature has
  proof, or has a stated reason it does not apply.** Proving one convenient
  entry point does not cover the others.
- Report an unreachable entry point with the attempted route and the unmet
  prerequisite. Do not report it as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the
user-visible behavior. It then uses exactly these four H2 sections in order:

1. `Sub-features` lists short, stable IDs with one line each. Include the
   states worth exercising (loading, empty, error, and feature-specific
   variants). Reproduction and resolution handoffs refer to these IDs.
2. `How to get to it (user POV)` lists every user entry point, grouped by
   surface. Two surfaces that share code but look different to a user are two
   entry points. A missing entry point is a map bug.
3. `Driving it with the repro environment` starts with `Preconditions:` and
   pairs each entry point with the test that exercises it, or with manual steps
   and the observable result when no test does.
4. `Gotchas` lists look-alike surfaces, per-harness differences, and traps that
   waste or invalidate a run.

`tests/dev/test_verify_omnigent_feature_map.py` checks this contract; see
[Keeping the map current](#keeping-the-map-current) for everything it enforces.

## Features

- [Composer](./composer.md) covers the session and new-session composers,
  the model/effort pill, the configuration gear, and slash commands.
- [Terminals](./terminals.md) covers the Chat/Terminal switcher, the agent
  terminal, shell terminals, and terminal reattachment.
- [Native harnesses](./native-harnesses.md) is the matrix of every native
  harness against launch, authentication, model and effort selection,
  approvals, resume, and terminal behavior, with Codex disconnect test boundaries.
- [Login and host authentication](./login-and-host-auth.md) covers
  `omnigent login`, host daemon credentials, Databricks auth modes, and embedded
  authentication for project writes.
- [Sessions](./sessions.md) covers the sidebar, whole-session and message forks,
  custom-agent targets, fork access checks, archive, reconnect, and resume.

## Seed scope

The five recipes above are the seed set. Completing this set means checking
the listed entry points against current source and recording how to verify
them, including explicit gaps. It does not mean that every recipe has been
driven live or that the whole product is mapped. Keep the backlog below
separate from a weekly correction pass; add another feature only as a separately
scoped change.

## Not yet mapped

These user-facing areas have no feature file yet, so this list is the checklist
of what to map next. An area that is only partly mapped is listed too. When you
map an area, remove it here in the same change.

**Web and apps** (UI test areas):

- Chat transcript, errors, and MCP status: `tests/e2e_ui/chat/` (only the
  composer and terminal journeys are mapped)
- Approvals, permission cards, and the inbox: `tests/e2e_ui/approvals/` (only
  native edit-tool approvals are mapped)
- Sub-agents and the agent info popover: `tests/e2e_ui/agents/`
- Files and the workspace browser: `tests/e2e_ui/files/`, `tests/browser_ui/files/`
- Comments: `tests/e2e_ui/comments/`
- Sharing and session permissions: `tests/e2e_ui/collaboration/`
- GitHub integration: `tests/e2e_ui/github/`
- Scheduled tasks: `tests/e2e_ui/scheduled/`
- In-app browser: `tests/e2e_ui/browser/` (session cookie sharing is covered in
  Sessions; other browser behavior remains unmapped)
- Desktop app: `tests/e2e_ui/desktop/`
- Web sign-in: `tests/e2e_ui/auth/`
- Branding and base-path deploys: `tests/e2e_ui/branding/`, `tests/e2e_ui/base_path/`
- Hotkeys: `tests/e2e_ui/hotkeys/`
- Message rendering: `tests/e2e_ui/messages/` (only per-harness render parity
  is mapped)
- Mobile layout: `tests/e2e_ui/mobile/` (only composer labels and terminal
  touch scroll are mapped)
- Host import review: `tests/e2e_ui/onboarding/`
- Visual snapshots: `tests/e2e_ui/visual/`
- Onboarding: `tests/e2e_ui/onboarding/`

**CLI commands:**

- Setup and diagnostics: `omnigent setup`, `omnigent config`, `omnigent doctor`,
  `omnigent diagnose`, `omnigent debug`, `omnigent usage`
- Install lifecycle: `omnigent upgrade`, `omnigent update`, `omnigent uninstall`
- Running agents and servers: `omnigent run`, `omnigent start`, `omnigent stop`,
  `omnigent server`, `omnigent attach`, `omnigent resume`, `omnigent session`,
  `omnigent import`
- Bundled agents and the Copilot harness: `omnigent polly`, `omnigent debby`,
  `omnigent copilot`
- Integrations, extensions, and remote sandboxes: `omnigent integration`,
  `omnigent extensions`, `omnigent sandbox`

**Partly mapped:** embedded authentication covers project writes and supporting
account-context tests; other resource APIs and real identity-provider journeys
remain unmapped. Native disconnect recipes cover Codex transport/startup and
browser stream recovery, not live reconnect across every harness. Network
interruptions for claude-native are tracked as scenario rows in
`docs/network-resilience.md`, driven by the resilience lab in
`tests/e2e/resilience/`. The
header-menu/custom-agent fork combination still needs a manual browser drive.

**No UI test lane yet:** Slack, the iOS and Android apps, policies and cost
budgets, MCP servers, sandbox providers, and smart routing.

## Keeping the map current

- **Every PR, enforced:** `tests/dev/test_verify_omnigent_feature_map.py` fails
  when a referenced test is renamed or removed, a feature file breaks the entry
  contract, a native harness has no matrix row, or a UI test area or CLI command
  is neither mapped nor listed under [Not yet mapped](#not-yet-mapped). It runs
  in CI and in the E2E UI workflow, so UI-only PRs are checked too.
- **Every PR, advisory:** when a PR changes user-facing code, the Polly review
  adds a non-blocking note if the change adds or removes an entry point that the
  matching feature file does not reflect.
- **Weekly:** the feature-map upkeep workflow is being prepared to check the
  seed files against source and run their referenced tests. Scheduled runs
  start in report-only mode; opening a correction PR requires an explicit
  apply run after validation. An incomplete or blocked pass must identify its
  gaps and must not publish corrections. A real product regression is reported,
  not documented away.
- **After review:** when a reviewer says a change missed a surface, add that
  surface to the feature file in the same change.
