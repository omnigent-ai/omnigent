# Host preference controls

On a server with the shared-preference store enabled, the host owner can call
`POST /v1/hosts/{host_id}/preferences`. The server supplies the authenticated
account/workspace identity; the host verifies its own login matches before
connecting. A host must advertise the `preferences` capability.

| Action | Input | Result |
| --- | --- | --- |
| `status` | none | Current adoption, cached shared state, machine overrides, pending edits |
| `connect` | none | Explicitly adopt existing shared defaults; never import local config |
| `sync` | none | Retry pending edits and refresh the host cache |
| `preview_import` | optional `plugins: ["claude", "codex"]` | Review portable defaults from this host's user config |
| `apply_import` | `preview` from import/restore | Queue the exact reviewed versions and synchronize |
| `preview_restore` | none | Review undoing the last acknowledged import |
| `set_machine` | `key`, `value` | Save a local override; `null` removes it |
| `discard` | `operation_id` | Discard the first blocked pending edit; does not undo a server commit |

Use `{"action":"status"}` to inspect. Inspection does not create a profile.
An import preview may verify the login and warm the local cache, but also leaves
adoption off. Applying or connecting is explicit. An unused shared workspace
still requires an initial edit or the CLI's `initialize-empty` choice.

Keys and values use the [shared API](shared-preferences-api.md) schema. Shared
writes use that existing versioned API; host controls handle local state and
imports. Ordinary session changes do not call either mutation path.

Imports omit commands, credentials, plugin installation paths and project policy.
Only portable user settings and explicitly requested native toggles are shown.
Saved machine overrides, the durable outbox, and import recovery records use the
same local SQLite cache as the CLI. Failed synchronization retains queued work.
Running sessions keep their frozen handoff.

Requests are capped at 8 MiB and four in-flight actions per connected host.
A disconnected/older host fails visibly. After a timeout, inspect status before
retrying a write: a lost reply does not establish whether it committed. Retrying
an import keeps the reviewed operation IDs and versions.

Browser controls and managed credential delegation are separate layers. These
controls require the host's own verified user credentials (or local single-user
mode); they never copy service credentials to a managed host. Rollout stays off.
