# Preferences and config defaults

Users can save local model and harness defaults, override them for a project,
and choose how an individual session runs. Native startup arguments also come
from local configuration. Shared workspace preferences have a resolver
and an opt-in server API for durable edits and conflicts. Explicit shared/machine scope controls and import previews are available through
the CLI and Settings → Shared preferences. Connected CLI launches and user-owned
hosts consume saved choices at startup. New-session composers prepare the selected
host’s defaults once, preserve unavailable choices, and offer explicit recovery.
Settings controls also use the owner tunnel on managed-token hosts without a
user login. Managed deployment wiring remains separate integration work. Pending edits
retry on host reconnect without polling between messages.
Claude/Codex native and SDK sessions also apply plugin toggles in private settings.
Native and SDK startup apply adopted model defaults and retain requested choices.
Saved native arguments are applied once by the runner, including browser launches.

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
- `saved-session-defaults`: persist the adopted portable defaults with the session;
  restart or move it to a fresh host without adopting later defaults. Forks and
  children inherit the saved choice; legacy sessions stay legacy.
- `shared-cli`: connect to an authenticated workspace, inspect shared/machine values,
  preview/apply an import or restore, retry edits and resolve conflicts.
- `shared-browser`: review/import all four groups, edit shared or machine values,
  remove overrides, resolve conflicts and safely retry uncertain saves in Settings.
- `shared-composer`: prepare defaults once per account/target; project and explicit
  choices win. Keep unavailable choices, retry a failed load or explicitly start
  with temporary defaults; carry the prepared snapshot into JSON/bundle creates.
- `shared-host-controls`: inspect adoption without changing it, preview portable
  host defaults, apply a reviewed import/restore, and set or remove machine overrides.
- `shared-plugins`: import explicit native user toggles, apply installed plugins
  independently in concurrent sessions, and retain unavailable selections with warnings.
- `shared-models`: apply the frozen model at native startup; retain an unavailable
  choice and show a startup error instead of replacing it with a default.
- `shared-arguments`: preserve ordered argument defaults on CLI and browser
  launches, append explicit session arguments once, and respect an explicit empty list.

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
key spelling, scopes and recovery. Settings provides the same explicit scope and import controls.

**Shared startup:** after `preferences connect` and the first explicit import/edit,
new CLI sessions follow the workspace. An unused workspace requires an import or
`preferences initialize-empty`. During an outage a verified cache is used with a
warning; a fresh machine offers Retry or `omnigent --temporary-preferences COMMAND`.
A deployment can opt new machines into discovery with `OMNIGENT_SHARED_PREFERENCES=1`;
the default is off. See the [startup guide](../docs/shared-preferences-startup.md).

**Shared browser settings:** open Settings → Shared preferences on web, desktop
or mobile. Both composer configuration menus link to it with Manage shared defaults.
Select a machine, preview its defaults, then Apply and follow; or explicitly use
existing workspace defaults. The Follow me scope changes shared defaults; This
machine saves an override. Review restore previews undoing the last import.
Competing edits show accepted and proposed values for an explicit choice. Merely
opening Settings, previewing or changing a session picker never publishes defaults.
The section is available only when the server advertises shared-preference support.

**New-session defaults:** on a server advertising shared preferences, open a new
session and choose a connected machine or managed target. Existing workspace
choices apply automatically; an unused workspace links to the one-time choice in
Settings. Machine preparation uses the authenticated owner tunnel and reads project
YAML without copying user credentials. No CLI offline profile is created by this
operation. Project and explicit session choices take priority. Unavailable Pi,
harnesses and models remain requested. A five-second wait offers Retry or explicit
temporary defaults; typing messages and reconnecting an existing session do not
refresh defaults. The exact launch is included in JSON and uploaded-bundle creates.

**Host preference controls:** on an opted-in server, an owner can call
`POST /v1/hosts/{host_id}/preferences` with `prepare`, `status`, `connect`, `sync`,
`preview_import`, `apply_import`, `preview_restore`, `set_machine`, or `discard`.
Status and import preview do not adopt the machine. Import reads only portable
user defaults and explicitly requested native plugin toggles. Older, disconnected
or differently authenticated hosts report a failure. See the
[host controls guide](../docs/shared-preferences-host-controls.md). Settings uses this route for machine controls and the shared API for versioned
shared edits.

**Shared plugins:** add `--plugins claude --plugins codex` to the import preview
to include explicit native user toggles. After adoption, native and SDK sessions use
private settings for installed plugins. Missing plugins remain requested and
produce a warning; installation stays local. See the
[plugin guide](../docs/shared-preferences-plugins.md).

**Shared models:** save `model` (or `opencode_model`) through the shared/machine
scope controls, then launch a new native session. Explicit launch, session and
agent choices still win. For an unavailable Claude/Codex selection, or an
unrepresentable Pi own-login selection, fix the provider or choose another
session model and retry; the saved default remains unchanged. See the
[model guide](../docs/shared-preferences-models.md).

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
and spawns a Python child that loads effective defaults and composes saved native
arguments before explicit session arguments. The server scenario
covers JSON and multipart metadata through SQL to the outbound frame. The native matrix
checks saved and temporary intent in all twelve adapters' multipart requests using simulated
HTTP. Those checks do not prove vendor model/plugin application or availability UI.
Component cases cover login changes, initial choice, startup timeout, frozen
session choices, bounded snapshots, and idle/bounded reconnect retries.

**Saved session defaults (own environment):**

```sh
uv run --no-sync pytest tests/stores/test_preference_snapshot.py \
  tests/server/integration/test_multipart_create_host_launch.py \
  tests/e2e/omnigent/test_shared_preferences_e2e.py::test_saved_session_preferences_resume_on_fresh_host --reruns 0 -q
```

The real-process journey creates a session, changes shared defaults, and runs the
same session on two isolated hosts, the second without a preference profile or
cache. Both runners initialize with the original snapshot and complete a scripted
provider turn. It checks saved intent and process handoff; the agent
either specifies its model or leaves it to the saved shared default. Both variants
must get replies from the selected scripted model on each host, after the current
shared default has changed. This checks SDK default-model selection and resume;
it does not establish live provider availability.
The runner and inference-routing component scenarios also check shared, machine,
project and explicit-agent precedence, unchanged source specs, session-only model
switches, and visible policy rejection across five SDK adapters.
Store cases cover single/split databases, bundle/registered create, reopen,
children, forks and oversized input. Account mismatch and temporary/adopted
contradictions are rejected at the server boundary. Legacy sessions resume with
no shared handoff; a runner with different saved preferences refuses reuse.

**Adopted native models (component/process boundary):**

```sh
uv run --no-sync pytest tests/test_preference_models.py \
  tests/runner/test_codex_model_pick_fallback.py \
  tests/runner/test_app_sessions_native_terminals_autocreate.py --reruns 0 -q
```

Existing runner scenarios now cover adopted versus legacy launches, saved model
application and no-reset errors for unavailable Claude/Codex and Pi selections.
Terminal processes and provider responses are simulated in these scenarios.
The shared-plugin recipe below supplies the real Codex process/config boundary.
It does not cover live availability for every vendor or managed Arclet startup.

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
  Adopted launches defer this merge to the runner for all native adapters, so
  browser launches use the same defaults. The harness-startup matrix checks
  repeated flags, explicit empty lists and legacy compatibility; the existing
  native sandbox/launch matrix also checks saved arguments reach the terminal
  spec. These are component checks, not real vendor-terminal launches.
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
explanations, project precedence, and unchanged files. CLI and host launches adopt
these layers through the startup recipe above. Before activation, complete browser,
managed-startup and per-harness availability journeys for each user entry point.

**Shared operations (own environment, real server processes):**

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_shared_preferences_e2e.py --reruns 0 -q -rs
```

The CLI/host scenario has three variants: separate CLI processes, a user host,
and a managed-token host without user credentials. All drive the same
import/override/restore assertions through real HTTP and isolated config/cache.
The managed-token case drops a committed edit's reply and disconnects the tunnel;
reconnect must finish the import without another write from the test or duplicate
revision increments. The token itself cannot access the shared HTTP API.
These are local processes, not a deployed managed sandbox. The host variants also
checks status and preview leave adoption off and omit local commands.
These scenarios cover: connect, preview/apply
a portable YAML/native-plugin import, set/remove a machine override, and restore
the import. Native toggles require the explicit `--plugins` options.

**New-session preparation and recovery:**

```sh
NODE_OPTIONS=--no-experimental-webstorage pnpm --filter web exec vitest run src/hooks/useNewSessionPreferences.test.tsx src/shell/NewChatDialog.projectPrefill.test.tsx
pnpm --filter web run build
uv run --no-sync pytest tests/e2e_ui/preferences/test_shared_preferences.py::test_composer_preserves_requested_choices_and_explicit_recovery --ui-skip-build --reruns 0 -q --video=on
```

Desktop and mobile browser clicks use a real server and host to preserve an
unavailable Pi choice, verify no preference writes or refresh on message typing,
and recover from an injected transport failure via Retry or temporary defaults.
No session is submitted in this browser scenario. Component tests cover exact
JSON create metadata, project/explicit precedence, unavailable choices, bounded
wait and account changes. The saved-session real-process recipe above obtains its
snapshot through the actual preparation route, then creates and resumes on two
hosts with scripted model replies. No live vendor or managed runtime is covered.

**Shared browser settings (own environment):**

```sh
uv run --no-sync pytest tests/e2e_ui/preferences/test_shared_preferences.py \
  --ui-skip-build --reruns 0 -q --video=on --output=/tmp/preference-ui-evidence
```

Build this checkout first. The journey starts an isolated server and host with
shared storage enabled and uses real browser clicks at desktop and mobile widths.
It verifies preview without adoption, all four imported groups, machine override
removal, competing edits, and restore preserving newer changes. Another-machine
edit is submitted through the real API. A dropped browser reply is injected after
a real commit; Retry reuses the exact mutation without incrementing revision.
No model request or managed deployment is involved. The shared composer-menu
component is covered by its existing frontend test; live composer launch adoption
and unavailable-choice recovery belong to the next launch slice.

**Host preference controls (component coverage):**

```sh
uv run --no-sync pytest tests/server/routes/test_preference_controls.py \
  tests/host/test_preference_controls.py tests/host/test_frames.py \
  tests/server/test_host_registry.py --reruns 0 -q
```

These checks cover ownership, authenticated identity, typed frame round trips,
old hosts, disconnect/timeout cleanup, bounded requests and concurrent actions,
invalid replies, and durable retry after an outage. The broker protocol tests
also cover request-scoped read/write grants, account and installation boundaries,
reviewed-import matching and uncertain delivery with a dependent queued edit.
The shared operations recipe above exercises real managed-token broker transport;
the browser recipe drives the same Settings controls through a user-owned host.

**Shared plugins (own environment):**

The real Codex scenario below also reads the effective model config for
each frozen workspace. Distinct model values survive concurrent startup. It does
not send a model request or establish provider availability.

```sh
uv run --no-sync pytest tests/test_preference_plugins.py \
  tests/inner/test_claude_sdk_executor.py \
  tests/harnesses/claude_native/test_claude_native_bridge.py::test_shared_plugins_are_private_invocation_settings \
  tests/harnesses/codex_native/test_skill_discovery.py::test_shared_plugins_isolated_through_real_app_server_startup --reruns 0 -q -rs
```

The Codex journey requires the native CLI. It installs a local fixture, starts
two real app-server child processes with opposite saved toggles for each native
and SDK variant, reads each loaded skill catalog, and checks the SDK menu agrees
and the original global config remains unchanged.
No provider/model is called. Claude checks native/SDK invocation settings, project/session
and managed precedence, missing-plugin intent and hermetic filtering; it does
not execute a vendor model or prove Claude loaded the plugin. Run the shared
operations recipe above for the actual CLI import and restore transport.

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
The [admission matrix](../tests/server/test_preference_requests.py) covers shared
HTTP/tunnel capacity, cancellation retaining worker slots, and account/workspace
rate isolation. Account deletion tests inject a mid-batch failure and verify
atomic rollback before retry. The [route matrix](../tests/server/routes/test_preferences.py) covers authenticated
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
Prepared host and CLI runner handoffs are disposable: the runner claims its
private file and host maintenance removes it after process exit. The saved-session
process journey checks actual runner ownership, cleanup after Stop, and a fresh
host resuming the unchanged server snapshot. Startup component tests cross a
reduced legacy file cap without blocking prepared launches and use a real child
process to verify live files survive while a dead owner's file is reclaimed.
Legacy unprepared handoffs stay frozen and are never evicted automatically.

The performance recipe uses v2 operations and accepts histories beyond receipt
retention. Setup is paced below the production account rate limit, outside timing;
measured samples expose overload without retries. A local SQLite burst does not
establish managed capacity or the load implied by a weekly active-user count.

Cache schema v4 records the negotiated protocol alongside namespace-bound host
adoption without creating a CLI login profile. Existing v1–v3 snapshots, receipts
and queued edits survive upgrade; older clients reject the newer cache version
instead of resetting it. The client matrix covers v1 server fallback, unattempted
edits after receipt compaction, and uncertain expired edits that remain queued
for explicit review. Broker tests retain exact lost-reply retries through typed
frames and allow only the observed revision to refresh on unattempted imports.

For latency and contention use the existing
[benchmark runner](../dev/benchmarks/omnigent/README.md#shared-preferences).
Record key/history/client counts, backend/driver, feature gate, revision,
latency percentiles, errors, and HTTP requests per operation. Compare receipt
history sizes and existing server journeys with the API off/on; label injected
network delay separately from a real WAN. Performance numbers do not replace
the correctness scenarios above.

## Gotchas

- Protocol v2 retains the latest 10,000 receipts. Expired uncertain edits are
  rejected visibly; review the current value, discard the pending edit, then
  explicitly save again if still needed. V1 clients require upgrade after 10,000
  total revisions. All server replicas must support v2 before enabling writers.
  Account cleanup uses 256-key statements within the atomic revocation transaction.
- Shared API writes are capped at 64 KiB before parsing (host import controls:
  8 MiB), with a five-second upload deadline. HTTP/tunnel/preparation share a
  16-worker admission limit and a per-process account/workspace rate limit.
  A busy response leaves queued edits intact; retry after congestion clears.
- Managed deployment, Universe wiring, Arclet startup,
  and live per-harness model availability remain unverified follow-ups. Startup adoption
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
- Read-only plugin inventory alone is not proof of independent enable/disable
  choices; use the native-plugin startup recipe. Session import review remains
  separate from the preference-import flow.
- Resolver tests and feature-map contract checks do not prove browser behavior,
  native policy enforcement, remote provisioning, or sync convergence.
