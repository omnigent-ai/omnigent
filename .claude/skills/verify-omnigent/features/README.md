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

## Baseline preconditions

- Launch the isolated environment from the [skill](../SKILL.md#launch). It
  starts a server, runner, and mock model server on private ports with its own
  config, data, Claude, and Codex directories.
- Run every drive through `verify-env run -- ...` (or, in CI,
  `python -m dev.repro_env exec -- ...`) so the process receives the
  environment's server, model, and runner URLs.
- Never drive an instance this run did not start. A host daemon, a developer
  server, or another agent's environment is out of bounds.

## Driving conventions

- Prefer an existing test over hand-driving. Each feature file names the tests
  that exercise its entry points. Run them with `--ui-skip-build`, recording
  enabled, and an output directory under the evidence location.
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

`tests/dev/test_verify_omnigent_feature_map.py` checks this contract, that every
referenced test still exists, and that the native harness matrix lists every
harness in the repository.

## Features

- [Composer](./composer.md) covers the session and new-session composers,
  the model/effort pill, the configuration gear, and slash commands.
- [Terminals](./terminals.md) covers the Chat/Terminal switcher, the agent
  terminal, shell terminals, and terminal reattachment.
- [Native harnesses](./native-harnesses.md) is the matrix of every native
  harness against launch, authentication, model and effort selection,
  approvals, resume, and terminal behavior.
- [Login and host authentication](./login-and-host-auth.md) covers
  `omnigent login`, host daemon credentials, and Databricks auth modes.
- [Sessions](./sessions.md) covers the sidebar, fork, archive, reconnect,
  and resume.
