# Preferences and config defaults

Users can save local model and harness defaults, override them for a project,
and choose how an individual session runs. Native startup arguments also come
from local configuration. Shared workspace preferences have a resolver
and an opt-in server API for durable edits and conflicts. Explicit shared/machine scope controls and import previews are available through
the CLI. Connected CLI launches and user-owned hosts adopt cached/shared defaults
at startup; browser controls and managed startup remain separate. Pending edits
retry on host reconnect without polling between messages.

## Sub-features

- `user-defaults`: set, list, and remove defaults on this machine; cover empty
  config, invalid keys, legacy scalar harness settings, and custom config homes.
- `project-defaults`: project YAML overrides user YAML for the model and the
  default harness; cover absent files, explicit clears, and scalar harness
  selection. Server selection and per-harness commands and arguments are read
  only from user YAML, and `config list` names any ignored project entries.
- `launch-choice`: explicit invocation choices take precedence over defaults;
  keep configured startup arguments ordered before invocation arguments.
- `inspect-host-defaults`: inspect the selected host's startup settings and
  installed plugins, including unavailable or older hosts.
- `session-choice`: choose a harness/model for a new session or change a
  supported setting in an existing session; keep the two composers distinct.
- `shared-scopes`: resolver only; requested values and their sources remain
  distinct across shared, machine, project, and session scopes.
- `shared-operations`: opt-in server API; read/save all four groups, retry an
  edit, remove a preference, inspect competing edits, and resolve them explicitly.
- `shared-client`: explicit Python client; preserve offline edits across process
  restarts, retry uncertain deliveries, refresh the cache, and surface conflicts. Cache records are versioned and bounded; lock contention
  and unsupported cache versions remain visible.

- `shared-startup`: refresh once for a new launch, use a verified workspace cache
  during an outage, or explicitly choose temporary defaults. Resume keeps its
  frozen handoff; reconnect retries pending edits with bounded jitter.
- `shared-cli`: connect to an authenticated workspace, inspect shared/machine values,
  preview/apply an import or restore, retry edits and resolve conflicts.

## How to get to it (user POV)

**Local CLI and files:** `omnigent config set --global model=<model>` or
`harness=<harness>` saves a user default. Omit `--global` to save in the current
project. Use `omnigent config list` to inspect the files and
`omnigent config unset [--global] model` to remove a value. Startup commands
and argument lists are configured in the user file's per-harness mappings;
`config list` names any project-file entries it ignores.

**Launch:** run `omnigent run` with an agent/default agent and optional model
or harness flags, or use a native command such as `omnigent codex` with
pass-through arguments. The native entry points and prerequisites are listed in
[Native harnesses](./native-harnesses.md).

**Web and desktop:** the new-session composer, in-session configuration menu,
native terminal controls, and fork dialog have separate selection paths; see
[Composer](./composer.md). Settings → Harnesses → select a host → a Claude or
Codex card/gear → Settings shows read-only startup configuration. The Plugins
tab shows inventory; both are covered in [Native harnesses](./native-harnesses.md).

**Shared operations API:** on an isolated server configured with
`shared_preferences_authority_id`, use `GET /v1/preferences/shared` and
`POST /v1/preferences/shared/operations` with the server's normal authentication.
The [API guide](../docs/shared-preferences-api.md) covers requests, conflict
resolution, identity, and limits. These endpoints do not adopt preferences into
launchers or turn on sync. Embedded apps opt in with `create_app(preference_store=...)`;
omitting an auth provider uses the reserved `local` user namespace.

**Embedded client:** use the [client API](../docs/shared-preferences-client.md)
with a private cache file, an expected account/workspace/deployment identity,
and the caller's authenticated HTTP client. Queue explicit edits locally, then
synchronize on reconnect. Standalone callers schedule synchronization. Connected CLI launches refresh at
startup; a user-owned host retries pending edits on reconnect.

**Shared scope controls:** `omnigent preferences --server URL connect` binds the
local cache without uploading existing defaults. Use `show`, `sync`, and
`set --scope shared|machine KEY JSON_VALUE`; `null` removes an override, `[]`
clears arguments, and `false` disables a plugin. A shared `set --resolve UUID`
resolves only the named conflict. `discard UUID` abandons the first pending edit.
Use `import SOURCE.yaml > plan.json`, review that file, then `apply plan.json`.
`restore > undo.json` previews undoing the most recent import; review and apply it
in the same way. The [CLI guide](../docs/shared-preferences-workflows.md) covers
key spelling, scopes and recovery. Browser controls remain unavailable.

**Shared startup:** after `preferences connect` and the first explicit import/edit,
new CLI sessions follow the workspace. An unused workspace requires an import or
`preferences initialize-empty`. During an outage a verified cache is used with a
warning; a fresh machine offers Retry or `omnigent --temporary-preferences COMMAND`.
A deployment can opt new machines into discovery with `OMNIGENT_SHARED_PREFERENCES=1`;
the default is off. See the [startup guide](../docs/shared-preferences-startup.md).

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

**Shared startup (own environment):** the default-agent test above has legacy,
connected, offline-cache and explicit temporary variants. Each launches an actual
CLI process and verifies the selected model by its mock-provider reply. It does
not run a vendor CLI or a managed host.

```sh
uv run --no-sync pytest tests/test_preference_startup.py \
  tests/host/test_connect.py::test_handle_launch_spawns_subprocess \
  tests/native/test_preference_launch.py \
  tests/server/integration/test_multipart_create_host_launch.py --reruns 0 -q
```

The host scenario decodes the wire frame, builds the real launch environment,
and spawns a Python child that loads effective defaults. The server scenario
covers multipart metadata through SQL to the outbound frame. The native matrix
checks temporary intent in all twelve adapters' multipart requests using simulated
HTTP. Those checks do not prove vendor model/plugin application or availability UI.
Component cases cover login changes, initial choice, startup timeout, frozen
session choices, bounded snapshots, and idle/bounded reconnect retries.

**Project and invocation compatibility (component tests):**

```sh
uv run --no-sync pytest tests/test_config.py tests/test_harness_startup_config.py \
  tests/cli/test_cli.py --reruns 0 -q
```

- `user-defaults`, `project-defaults`: the config loader tests cover file
  precedence and clears; CLI tests cover writes, removal, inspection, and scalar
  harness migration. Removing a project value allows the user default to apply.
  The adopted-preference matrix also covers aliases such as `claude` for
  `claude-sdk`: project per-harness entries never replace user arguments,
  and a session choice can still override.
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

**Shared operations (own environment, real server processes):**

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_shared_preferences_e2e.py --reruns 0 -q -rs
```

The CLI scenario also crosses separate command processes: connect, preview/apply
a portable YAML import, set/remove a machine override, and restore the import.

The server scenario drives two isolated CLI servers sharing a SQLite database through actual
HTTP requests (or another isolated backend via `OMNIGENT_TEST_DB_URI`). It checks
conflicting edits, explicit resolution, retries, deletion, and persistence after
restart. Key/value and authentication matrices stay in the store and route tests.
It also walks small pages across replicas, resumes a cursor after restart,
and rejects continuation after another edit; a stable traversal has no missing
or duplicated preferences.
No services or providers are mocked; no model is called. This verifies the local
OSS API, not managed deployment or actual native application of preferences.
The store matrix can also run on PostgreSQL, MySQL, and CockroachDB when
`OMNIGENT_TEST_DB_URI` names an isolated backend. CI's external-database lanes
run `tests/stores`; the real-process test is an explicit run on those backends:

```sh
uv run --no-sync pytest tests/stores/test_preference_store.py \
  tests/server/routes/test_preferences.py tests/test_preference_sync.py --reruns 0 -q
```

The [store matrix](../tests/stores/test_preference_store.py) covers bounded
pages, concurrent edits, limits, atomic rejection, and stable retry receipts.
The [route matrix](../tests/server/routes/test_preferences.py) covers authenticated
and no-auth embedding, identity validation, and HTTP status mapping. The
[protocol tests](../tests/test_preference_sync.py) own key/value validation.
Use the process recipe above for replica and restart transport.

For later slices, continue live verification of each newly connected path on an
isolated deployment. Record the candidate/runtime versions, configuration,
observed results, any mocked services, and remaining entry-point gaps.

**Durable client (own environment):** the same server test file also runs
`test_durable_preference_clients_offline_reconnect_and_restart`. It launches
separate client processes with private caches, queues all four groups offline,
resolves competing edits through two server replicas, injects a lost response
after a real commit, stops both servers, and starts a fresh replica to verify
retry/deletion convergence. It also rejects a duplicate conflict resolution,
explicitly discards that blocked edit, and delivers the remaining queued work.
The lost-response fault is injected; HTTP and SQL are real. This is local OSS
client/API evidence, not automatic host sync, launcher adoption, or Arclet proof.

```sh
uv run --no-sync pytest tests/test_preference_client.py --reruns 0 -q -rs
```

The process client requests two-row pages to exercise full traversal before
adopting a snapshot, including after server restart. The component matrix covers
later-page disconnects, changed revisions/identities, duplicate entries, repeated
cursors, and malformed pages: the prior cache and outbox remain intact, and a
fresh retry can converge. These cases extend the same `shared-client` recipe.

The component matrix also covers causal offline edits, malformed or stale replies,
identity changes, cancellation, local writers, sync locking, and outbox limits.
Its HTTP transport is simulated; the mapped process test covers real transport.

For latency and contention use the existing
[benchmark runner](../dev/benchmarks/omnigent/README.md#shared-preferences).
Record key/history/client counts, backend/driver, feature gate, revision,
latency percentiles, errors, and HTTP requests per operation. Compare receipt
history sizes and existing server journeys with the API off/on; label injected
network delay separately from a real WAN. Performance numbers do not replace
the correctness scenarios above.

## Gotchas

- Before broad activation, design receipt retention/compaction and bounded
  account-deletion batches. At 10,000 consumed operations, new edits and conflict
  resolutions stop permanently; reads and identical retries still work.
- Request parsing currently has no route-specific body-size or rate limit.
  Add these before broad activation; the value cap applies after JSON parsing.
- Managed deployment, Universe wiring, Arclet startup, native plugin isolation,
  and per-harness model availability remain unverified follow-ups. Startup adoption
  carries portable intent; each adapter must still apply and report its capabilities.
- `--global` means this machine's user YAML, not a shared workspace write.
  Reading or editing YAML does not opt into shared preferences.
- `config list` displays user and project defaults under their file paths;
  seeing both rows does not mean both values are active.
- Server-stored project defaults used by the new-session composer are separate
  from local `.omnigent/config.yaml`. The stubbed browser requests in
  `tests/e2e_ui/start_session/test_project_config_prefill.py` exercise the former,
  not reading host YAML or syncing workspace preferences.
- A missing setting, an explicit project clear, an empty argument list, and a
  disabled plugin have different meanings. A project harness, scalar or
  `default`, only selects the default; per-harness fields stay user-owned.
- Startup commands and credentials stay local. Selecting a harness or copying
  its arguments does not install the binary or make referenced paths available.
- Read-only plugin inventory is not proof that enable/disable preferences can
  be applied independently in concurrent workspaces. Session import review is
  also separate from the future preference-import flow.
- Resolver tests and feature-map contract checks do not prove browser behavior,
  native policy enforcement, remote provisioning, or sync convergence.
