# Host accounts

A host account tells the server which account a connected machine already uses
for a native Claude or Codex harness. With that map, the session menu can move a
session to another account, machine, model or reasoning effort in one dialog.
The map is optional. When it is not configured, the server and the web app
behave as before.

Here an "account" is an operator label for an account on a host route. It is not
a login to Omnigent, and it is not the Codex ChatGPT plan sign-in that Settings
shows under harness accounts.

## Configure the registry

Set `OMNIGENT_HOST_ACCOUNTS_FILE` to a JSON file and restart the server. The
server reads the file at startup and refuses to start if the file is unreadable
or malformed. Leave the variable unset to keep the default behavior.

The registry names routes that already exist. It holds no credentials,
credential paths, launch profiles or model lists, and it never signs a host in
to an account. Each host must already select the declared account for its
harness.

```json
{
  "version": 1,
  "accounts": [
    {
      "id": "sample-openai",
      "label": "OpenAI personal",
      "provider": "openai",
      "harness": "codex-native",
      "placements": [
        {"host_id": "sample-host-one", "machine_id": "laptop", "machine_label": "Laptop"},
        {"host_id": "sample-host-two", "machine_id": "workstation", "machine_label": "Workstation"}
      ]
    }
  ]
}
```

- `provider` is `anthropic` with `claude-native`, or `openai` with `codex-native`.
- Each account `id`, and each host and harness pair, must be unique.
- `host_id` is the id shown for a host in `GET /v1/hosts`. Several host ids may
  share one `machine_id` when one physical machine runs more than one host.
- A host process still decides how it signs in. Changing the registry does not
  move a login.

## Endpoint

`GET /v1/execution-topology` returns the registry for the caller's own
non-sandbox hosts: `configured`, `machines` (`id`, `label`), `accounts` (`id`,
`label`, `provider`, `harness`) and `bindings` (`host_id`, `machine_id`,
`account_id`, `harness`, `available`, `reason`). A binding is identified by the
host id and the harness, because one host can serve both a Claude and a Codex
account. `reason` is "Machine is offline" or "Harness is not ready" when
`available` is false. Without a registry it returns `configured: false` and empty
lists.

`GET /v1/hosts/{host_id}/harnesses/{harness}/model-options` accepts an optional
`account_id`. With a registry configured it rejects a host and harness pair that
is not registered, or that belongs to another account.

## What the server checks

With a registry configured, native Claude and Codex sessions are checked here:

| Action | Check |
| --- | --- |
| Create a session on a host (JSON, bundle or from an agent) | The host and harness are registered. |
| Launch a runner on a host | The route is registered, and the model and effort exist in the host's live catalog. |
| Change model or effort of a host-bound session | The same catalog check. |
| Move to another account, machine, model or effort | The destination passes the same checks, and the harness stays the same. |

Model lists and supported efforts always come from the selected host and
harness. A Claude catalog without an effort list uses Omnigent's Claude effort
names. Other harnesses are never affected.

The registry routes sessions; it does not sandbox them. These are not checked:

- A runner that already exists is not re-checked against the registry.
- Automatic relaunch of a stopped session reuses the session's host.
- In-terminal commands that attach a runner to a session (`/clear`, `/switch`)
  are not checked, except while a move is pending, when they are refused.
- Launch arguments and a host's own configuration are not inspected.

A session keeps its harness. Moving between Claude and Codex is a fork.

## Moving a session

In the composer's host menu, **Switch model or account...** opens one dialog:
Machine, then Account, then Model and Effort. Only accounts with the session's
harness are offered. The Machine control appears when an account has placements
on more than one machine. The button reads **Move session** when the machine
changes. A sub-agent session cannot be moved on its own; move its parent. A
session that has no host binding (started from the CLI) cannot be moved.

A move is recorded on the server before the old runner is released:

1. `PATCH /v1/sessions/{id}` with `runner_id: ""`, `account_id`,
   `target_host_id`, `target_harness`, `target_model`, `target_effort` and
   `target_workspace`. In one transaction the server stores the destination as
   `account_transition` and detaches the runner, then stops the old runner in the
   background. The response returns the destination and its `id`.
2. The client sets the model and effort with `transition_id` set to that `id`.
3. `POST /v1/hosts/{host_id}/runners` with `account_id` and `transition_id`
   launches the destination. The server stops any earlier launch attempt and
   clears the marker when the host acknowledges the launch.

A change of model or effort on the same host and account is a plain `PATCH`
with no marker.

While a move is pending, the session accepts no events, no automatic resume and
no runtime edits without the matching `transition_id`, and no runner other than
the move's own launch can attach. Only that launch runner can send events. Title, label and archive edits still work, and
a pending session can be forked. A move that fails or is interrupted keeps its
marker, and the dialog offers to resume it or cancel it.

To cancel, the owner sends `PATCH /v1/sessions/{id}` with `transition_id` and
`abandon_transition: true`. The server stops the launch attempt, clears the
marker and leaves the session detached, so any machine can be chosen next. The
model and effort return to their values from before the move. An attempt that a
failed launch already unbound is stopped on a best-effort basis, so a cancel
never waits for an offline host. The request takes no other fields.

## Storage and rollback

Migration `7a5b2074da28` adds the nullable `account_transition` column
(`VARCHAR(8192)`) to `omnigent_conversation_metadata`. The store caps each value
at 8192 bytes. It holds the destination, the previous host and runner, and the
runner of the latest launch attempt. Deploy the migration before servers that
write the column; older servers ignore it.

Before running older code, check that no move is pending:

```sql
SELECT EXISTS (
  SELECT 1 FROM omnigent_conversation_metadata WHERE account_transition IS NOT NULL
);
```

Older code does not enforce a pending marker. If the result is true, restart the
new version and finish or cancel those moves. An explicit Alembic downgrade
refuses to run while a marker is pending.
