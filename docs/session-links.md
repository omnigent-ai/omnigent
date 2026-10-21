# Session link tools

A manager can send work to an existing session through a human-approved,
directed link. Each operation uses the initiating person's current permissions
on both sessions. Sharing a manager never lends its owner's authority.

The feature is experimental and disabled by default. Local verification covers
OpenAI SDK and Codex native 0.162.0 with scripted providers. The separate CI
prerequisite pins that Codex release, including the setup action's callers.
Codex 0.139.0 lacks the paginated input-receipt APIs used here and linked calls
fail closed. A verified human approval credential boundary remains a prerequisite
for multi-user activation; local header authentication does not validate it.

## Human connection

Open **Session links** in A's header, choose a session you own, and select
**Connect**. The server stores A→B only if you own both endpoints in the same
tenant workspace. The dialog shows incoming and outgoing directions. Canvas
shows a selectable arrow when both sessions are visible on the board. Reverse
directions are separate grants and can be inspected independently.

Either current endpoint owner can disconnect, including after ownership changes.
The Links dialog shows inaccessible endpoints as “Restricted session” so an
owner can remove a stale link without reading the other conversation. Ownership
and archive changes invalidate approvals even if the previous value is restored.
Operations also recheck account generations and the current runner binding.

## Tools

| Tool | Arguments | Behavior |
|---|---|---|
| `sys_session_links` | Optional `incoming`, `after`, `limit` | Lists approved directions the requesting person can see. An inactive link needs fresh approval. |
| `sys_session_delegate` | `target_id`, `message` | Sends ordinary input to B and returns a durable request ID and delivery state. |
| `sys_session_task` | `request_id`; optional `after`, `limit` | Polls delivery, current worker status, and addressed replies. Turn completion alone is not a reply. |
| `sys_session_reply` | `request_id`, `message` | Sends an explicit reply to the original manager through the original link. |

Pages contain at most 100 records; the default is 50. Follow `next_cursor` until
it is null, even when viewer filtering leaves an empty page. A message is at
most 8,000 UTF-8 bytes. The runtime supplies retry IDs; the model cannot choose
the source session, requester, or approval authority. There is no approval tool.

Example user journey:

1. In A, ask: “Ask B to check its open PR and report what needs rebasing.”
2. A delegates through its approved outgoing direction. B sees the initiating
   person, source session and request ID beside the request.
3. B continues its normal conversation. In B, ask: “Send that result back to A
   using the request ID.” Only that addressed reply is copied.
4. A can poll the request or read an available inbox notification. Ordinary
   output stays in B. General turn-end subscriptions are deferred.

## Requester and conversation behavior

The server captures immutable input references. A trusted runner attests which
inputs its harness accepted into the turn; the server resolves their human
identity and rechecks permissions. Model arguments, message text, and mutable
attribution do not establish authority.

For A→B→C, both A→B and B→C must be approved, and the original requester must
have their own current edit access at each hop. Someone who can use A and B
but cannot use C is refused before C receives new work. Limits are eight hops
and 64 linked requests originating from one input, including sibling branches.

Busy workers retain their harness's normal behavior. OpenAI SDK queues a
follow-up for a later turn; it keeps that input's requester separate. Codex
native can consume a steer in the current turn. If one turn consumes inputs
from different people, linked operations fail closed until a fresh unambiguous
turn. Missing native receipts and unsupported terminal input also fail closed.

## Replies, retries, and disconnecting

Replies sent to A become part of A's conversation. Anyone with current or later
read access to A can read those copies. They may contain material from B's
existing context; sharing A consents to that flow. A link grants no direct
transcript or terminal access to B. New reverse work requires B→A separately.

Disconnecting stops new requests, polls, and unpublished replies. Work already
accepted into B's normal input path continues; use B's ordinary Stop control
to interrupt it. Retained copies in A remain visible. A pending handoff has a
20-second runner acceptance lease; Disconnect can wait for it to settle or
expire. Late refused input may remain visible as a delivery record but must
not re-enter harness instructions during recovery or an authorized fork.

Requests and replies have durable idempotency receipts. Repeating a transport
operation cannot enqueue another copy, including after reconnecting the same
pair. An uncertain result is not permission to create a new operation. The
manager's inbox notification is best effort and process-local; copied history
and `sys_session_task` are the durable sources. This MVP does not launch an
offline worker or provide automatic delivery retries.

## Verification and rollout order

Use the [feature recipe](../feature-map/session-links.md) for browser and
real-process commands, and the [API reference](../omnigent/server/API.md#directed-session-links-experimental)
for HTTP contracts. Local mock-provider runs do not establish live-provider,
native desktop, or deployed authentication compatibility.

Land schema independently before application consumers: the additive migration
adds five metadata tables and older binaries ignore them. Keep those tables
and receipts through application rollback. A schema downgrade deletes history
and is only appropriate for disposable test databases. The next review units
are requester/runtime/delivery, then Links UI and Canvas. Activation requires
the human-approval authentication decision and combined acceptance evidence.
