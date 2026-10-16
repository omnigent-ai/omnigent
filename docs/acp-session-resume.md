# ACP session resume

The generic ACP harness uses the agent's own session history when it advertises
the stable `loadSession` capability. Omnigent keeps its existing transcript for
display, while the native ACP agent owns execution context, tools, checkpoints,
and compaction. This also works with a local stdio relay to a remotely hosted
agent: the relay must return the remote agent's original session ID.

## Creating and restoring a session

1. The runner reads `external_session_id` from the existing conversation API on
   every turn, including after a host restart.
2. A new ACP session is created through `session/new`. Before any `session/prompt`,
   the harness asks the runner to persist the returned ID. The runner uses the
   existing `PATCH /v1/sessions/{id}` API, checks that the response confirms the
   same ID, and acknowledges the checkpoint on the existing harness event channel.
3. After a restart, the harness uses `session/load` with that original ID. It
   consumes the agent's load-time history replay without rendering it as new
   output or sending Omnigent's transcript back as a prompt prefix. Only the new
   user turn goes to `session/prompt`.

No new database, conversation API, or server migration is required. The existing
server must support `external_session_id` (including Omnigent 0.16). Runner and
harness communicate this capability through the presence of the private
`native_session_id` message field. An older runner that omits the field keeps
the previous in-memory behavior; agents without `loadSession` also keep that
behavior and do not receive unsupported load requests.

## Failure, cancellation, and profile changes

| Situation | Behavior |
| --- | --- |
| The reference cannot be read or persisted | Fail before prompting; do not create an untracked execution turn. |
| An existing session cannot be loaded or support disappears | Preserve the saved ID and fail; do not silently create a replacement session. |
| Cancellation or transport EOF while persistence is pending | Send no prompt. If the database write committed, the next turn reads that reference afresh and loads it. |
| Cancellation during a prompt | Use standard `session/cancel` with the native ID and retain the durable reference for reconnection. |
| An agent requests a tool, permission, or filesystem action while replaying history | Reject the request and fail before prompting; replay does not execute actions. |
| An executor cannot be closed safely | Fail before starting a replacement; unconfirmed teardown does not permit overlapping native sessions. |

Keep the configured ACP endpoint, command, and agent profile pinned for the life
of the conversation. The native ID alone is not a portable cross-provider
binding. Changing the profile requires a new conversation or a fork into
another agent. In-place agent switching is unavailable; an existing conversation
keeps its original native reference. This change does not add automatic native
history migration or profile identity storage.

## Pending native input

Standard ACP permission requests during a prompt continue through Omnigent's
existing policy and human-review path. Native application forms remain in the
application that owns their checkpoint; they are never automatically accepted.
For agents that return the CAIPE pending-input metadata (`caipeStatus` or
`caipePendingInterrupt` with `caipeConversationUrl`), Omnigent shows an actionable
pending-input error and preserves the session. After the user resolves the form
in that application, a later turn reloads the same native session. An ordinary
ACP `refusal` receives a refusal error without being described as a missing
human approval.

## Verification

Configure an ACP agent that supports `session/load`, select it in a conversation,
and send a prompt. Read the conversation through the existing session API and
confirm that `external_session_id` is populated. Restart the local host, reopen
the same conversation, and send a follow-up. Confirm that the agent retains the
first turn's context and the UI does not append its replayed transcript again.

For a native application with forms, trigger a pending approval, follow its
conversation link, and resolve the form there. Return to Omnigent and send the
next turn; it should use the original ID. Separately, cancel a running prompt,
reconnect, and confirm that the conversation reference stays unchanged. To use
another agent, start a new conversation or fork into that agent and confirm that
it receives its own native session reference.

Automated coverage exercises actual JSON-RPC stdio plus the runner-to-HTTP-harness
checkpoint round trip:

```bash
uv run --no-sync pytest tests/inner/test_acp_native_session.py \
  tests/runtime/harnesses/test_native_session_checkpoint.py \
  tests/runner/test_acp_native_session.py -q
```
