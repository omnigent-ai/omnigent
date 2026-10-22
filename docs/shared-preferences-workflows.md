# Explicit workspace preference controls

These commands bind to an opt-in server and save user intent. Automatic launch
adoption and native plugin application are implemented separately; this slice
provides the scoped controls and durable import/restore behavior.

```sh
omnigent preferences --server URL connect
omnigent preferences --server URL show
omnigent preferences --server URL set --scope shared harness '"pi-native"'
omnigent preferences --server URL set --scope machine model '"my-experiment"'
omnigent preferences --server URL set --scope machine model null
```

Use your configured `server` to omit `--server`. Normal login credentials and
workspace routing apply. Connection stores the authenticated deployment/user/
workspace identity and host installation ID; it never uploads YAML automatically.
Machine overrides are isolated by that identity and installation. Ordinary
session switches never invoke these writes.

Keys are `model`, `opencode_model`, `harness`, `args/HARNESS`, or
`plugin/ECOSYSTEM/SOURCE/NAME` (percent-encode slashes in plugin components).
Values are JSON. `false` explicitly disables a plugin, `[]` clears configured
arguments, and `null` removes the selected scope's override. Shared edits are
queued durably before sync. An error leaves them visible in `show`; run `sync`
to retry. Use `set --scope shared --resolve CONFLICT_ID KEY VALUE` for an explicit
conflict decision. `discard OPERATION_ID` abandons the first pending edit without
undoing a possible server commit.

## Import and restore

```sh
omnigent preferences --server URL import ~/.omnigent/config.yaml > plan.json
# Inspect plan.json: values, old values and expected versions.
omnigent preferences --server URL apply plan.json
omnigent preferences --server URL restore > undo.json
# Inspect undo.json before applying it.
omnigent preferences --server URL apply undo.json
```

Import extracts portable YAML model/default-harness/argument settings. Executable
commands and credentials stay local; native plugin sources need their adapters.
Absent entries are preserved. All supported groups, including plugin toggles and
explicit clears, can also be supplied through the typed `preview_import` API.

Applying a plan queues exactly its reviewed values and versions. A remote change
between preview and apply becomes a visible conflict. Machine overrides survive
imports. Pending edits must be synchronized or explicitly discarded first.

The latest nonempty import retains its prior values and received results on this
machine. That restore record and the queued patch commit atomically. A restore
uses each applied result's version; later shared edits conflict instead of being
silently overwritten. Conflicted/no-op import entries need no undo. Unacknowledged
or discarded entries block restore until investigated. Applying a new nonempty
plan replaces the previous restore record; an empty preview does not erase it.

Local schema version 2 adds bounded machine and import records. Version 1 upgrades
in one transaction without changing its binding, cache or pending edits. Old
clients reject version 2 instead of dropping state. This extends the local-store
design proposed in the [client guide](shared-preferences-client.md).

## Verification

Use the `shared-cli` recipe in the [feature map](../feature-map/preferences.md).
Component tests cover concurrent edits, exact preview versions, restore conflicts,
scope isolation and migration. The process scenario drives real CLI commands
against an isolated server and SQLite, with no model calls or global settings.
