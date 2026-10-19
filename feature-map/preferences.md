# Preferences and config defaults

Users can save local model and harness defaults, override them for a project,
and choose how an individual session runs. Native startup arguments also come
from local configuration. Shared workspace preferences currently have only a
resolver foundation: automatic sync and shared/machine scope controls are not
available through the CLI or UI yet.

## Sub-features

- `user-defaults`: set, list, and remove defaults on this machine; cover empty
  config, invalid keys, legacy scalar harness settings, and custom config homes.
- `project-defaults`: project YAML overrides user YAML; cover absent files,
  per-harness fields, explicit clears, and scalar harness replacement.
- `launch-choice`: explicit invocation choices take precedence over defaults;
  keep configured startup arguments ordered before invocation arguments.
- `inspect-host-defaults`: inspect the selected host's startup settings and
  installed plugins, including unavailable or older hosts.
- `session-choice`: choose a harness/model for a new session or change a
  supported setting in an existing session; keep the two composers distinct.
- `shared-scopes`: foundation only; requested values and their sources remain
  distinct across shared, machine, project, and session scopes.

## How to get to it (user POV)

**Local CLI and files:** `omnigent config set --global model=<model>` or
`harness=<harness>` saves a user default. Omit `--global` to save in the current
project. Use `omnigent config list` to inspect the files and
`omnigent config unset [--global] model` to remove a value. Startup commands
and argument lists are configured in the files' per-harness mappings.

**Launch:** run `omnigent run` with an agent/default agent and optional model
or harness flags, or use a native command such as `omnigent codex` with
pass-through arguments. The native entry points and prerequisites are listed in
[Native harnesses](./native-harnesses.md).

**Web and desktop:** the new-session composer, in-session configuration menu,
native terminal controls, and fork dialog have separate selection paths; see
[Composer](./composer.md). Settings → Harnesses → select a host → a Claude or
Codex card/gear → Settings shows read-only startup configuration. The Plugins
tab shows inventory; both are covered in [Native harnesses](./native-harnesses.md).

**Shared scopes:** there is no user entry point yet for importing/reimporting
preferences, saving shared or machine defaults, removing a machine override,
resolving conflicts, or continuing with temporary defaults. Add the actual
routes and their journey tests here as those controls are implemented.

## Driving it with the repro environment

Preconditions: use an isolated config home and project; never edit your usual
config to verify this recipe. The CLI subprocess tests below own their
environment and use a local mock model where needed. They need no browser or
shared repro instance. For the linked browser/native recipes, follow their
`verify-env` and harness prerequisites.

**Local defaults and launch (own environment):**

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_config_defaults_e2e.py --reruns 0 -q
```

This verifies config write/list/unset across processes, rejection of an unknown
key without persisting it, and a default-agent launch with a mock reply.

**Project and invocation compatibility (component tests):**

```sh
uv run --no-sync pytest tests/test_config.py tests/test_harness_startup_config.py \
  tests/cli/test_cli.py --reruns 0 -q
```

- `user-defaults`, `project-defaults`: the config loader tests cover file
  precedence and clears; CLI tests cover writes, removal, inspection, and scalar
  harness migration. Removing a project value allows the user default to apply.
  The adopted-preference clear matrix also covers aliases such as `claude` for
  `claude-sdk`: clearing the entry or its arguments removes inherited arguments,
  a command-only override keeps them, and a session choice can still override.
- `launch-choice`: CLI dispatch tests check explicit choices and Pi argument
  ordering. The native startup tests check Claude/Codex pass-through arguments
  remain unmerged until the runner, so the configured base is applied once.
  These are component checks, not real vendor-terminal launches.
- `inspect-host-defaults`, `session-choice`: use the linked Native harnesses and
  Composer recipes for the actual settings, terminal, new-session, in-session,
  fork, and mobile surfaces. Their results do not establish shared-preference
  persistence or isolation across machines.

**Shared scopes (component coverage only):**

```sh
uv run --no-sync pytest tests/test_preferences.py tests/test_config.py --reruns 0 -q
```

The matrix checks precedence for all four groups, explicit disabled plugins
and empty argument lists, removal of overrides, and rejection of mismatched
workspace/account/machine contexts. The YAML-to-launch test checks source
explanations, project precedence, and unchanged files. No real client currently
adopts these layers. Before activation, add cross-machine, restart, conflict,
offline, native-plugin, and managed-startup journeys with explicit coverage for
each user entry point.

**Next implementation slice:** include live verification of the newly connected
preference path on an isolated deployment. Record the exact candidate and runtime
versions, enabled configuration, user action, and observed result, including
persistence/reload and affected consumers where implemented. Identify the mapped
entry points exercised, any mocked services, and remaining gaps. Supplying layers
directly to the resolver does not verify the live path.

## Gotchas

- `--global` means this machine's user YAML, not a shared workspace write.
  Reading or editing YAML does not opt into shared preferences.
- `config list` displays user and project defaults under their file paths;
  seeing both rows does not mean both values are active.
- Server-stored project defaults used by the new-session composer are separate
  from local `.omnigent/config.yaml`. The stubbed browser requests in
  `tests/e2e_ui/start_session/test_project_config_prefill.py` exercise the former,
  not reading host YAML or syncing workspace preferences.
- A missing setting, an explicit project clear, an empty argument list, and a
  disabled plugin have different meanings. A scalar project harness replaces
  the whole lower harness value; a mapping merges per field.
- Startup commands and credentials stay local. Selecting a harness or copying
  its arguments does not install the binary or make referenced paths available.
- Read-only plugin inventory is not proof that enable/disable preferences can
  be applied independently in concurrent workspaces. Session import review is
  also separate from the future preference-import flow.
- Resolver tests and feature-map contract checks do not prove browser behavior,
  native policy enforcement, remote provisioning, or sync convergence.
