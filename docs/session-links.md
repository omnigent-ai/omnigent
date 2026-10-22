# Owner session communication

Enable **Session communication** in a session header to let your manager send
work to your other sessions. The switch is personal to your account and tenant
workspace, defaults to Off, and covers all current and future sessions you own.
Shared sessions remain eligible. Every operation checks the original requester's
current ownership of both endpoints; an editor cannot borrow the owner's switch.

Owner UI or CLI credentials may change the switch, including an agent holding
those credentials. Ordinary runner credentials cannot. Magic login remains
supported. The experimental server feature `session_links` is disabled by default.
Universe integration is a separate, near-term follow-up.

## User journey

1. Open **Session communication** in manager A and select **Allow communication**.
2. Ask A: “Ask B to check its open PR and report what needs rebasing.”
3. B receives ordinary input in its existing conversation. Its context, steering,
   parentage and existing child workflows remain intact.
4. Ask B to send an addressed reply using the request ID. Only that reply is
   copied to A; ordinary worker output stays in B.
5. Canvas shows communication activity between visible sessions. Arrows record
   requests; they do not grant permission or imply successful completion.
6. Select **Turn off communication**. New requests, polls and unpublished replies
   stop. Accepted work continues, and existing copies remain readable.

Anyone who can read A, now or after later sharing, can read replies copied there.
Those replies may contain information from B's existing context. Sharing A does
not grant B's full conversation, files or terminal access.

## Agent tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `sys_session_links` | Optional `incoming`, `after`, `limit` | Actual communication activity visible to the requester. Use `sys_session_list` for discovery. |
| `sys_session_delegate` | `target_id`, `message` | Durable request ID and delivery state. |
| `sys_session_task` | `request_id`; optional `after`, `limit` | Delivery, current worker status and addressed replies. |
| `sys_session_reply` | `request_id`, `message` | Explicit reply copied to the request's manager. |

The runner supplies retry IDs and immutable input references outside model
arguments. The source, requester and authorization cannot be chosen by the model.
Messages are limited to 8,000 UTF-8 bytes; pages to 100 records (default 50).
Follow `next_cursor` even when visibility filtering leaves an empty page.

## Authority, delivery and recovery

The server captures identity from signed credentials. The bound runner reports
consumed input IDs, and the server resolves the requester from immutable records.
Text and display labels are not authority. A→B→C retains the original requester;
B→C fails if that requester does not own C. New reverse work independently checks
the same ownership and switch rules. Limits are eight hops and 64 requests per
original input, including sibling branches.

OpenAI SDK queues a busy follow-up for a later turn. Codex native can steer the
current turn; consumed client IDs must match native RPC receipts. Missing or
mixed-user authority refuses orchestration. Unverified terminal input cannot
start linked work. Other native harnesses keep their existing tools without
advertising the new orchestration tools.

The switch uses existing preferences storage. Four new metadata tables retain
endpoint epochs, input authority, requests and replies. These make ownership
changes, idempotency and recovery durable; no pairwise grants table is needed.
Message bodies use existing artifact and conversation storage. Named operations
and workspace keys apply to every record.

Turning Off invalidates pending authorization and waits for claimed handoffs to
settle or expire (a 20-second acceptance lease). Late refused input may remain
visible as a delivery record, but cannot become recovery instructions or forked
instructions. Reenabling does not revive an old request. Ownership, archive and
runner-binding changes also invalidate old authority.

A transport retry cannot enqueue duplicate work. An uncertain result must not be
resent as a new operation. Polling and copied history survive process restarts;
inbox notifications are best effort. The MVP does not launch offline workers,
automatically retry delivery, or subscribe to general turn-end notifications.

## Verification and future work

See the [feature recipe](../feature-map/session-links.md) and
[HTTP contract](../omnigent/server/API.md#owner-session-communication-experimental).
Accounts-authenticated process/browser tests use scripted models; they do not
prove live-provider, production-database or actual Electron/mobile behavior.
Codex native verification uses 0.142.0, matching CI; missing receipt APIs fail
closed. The auth migration requires old clients to sign in or refresh and
runners to reconnect for 0.20.0.

The consolidated PR includes auth, migration, runtime and UI. Keep additive tables
and receipts through application rollback; schema downgrade destroys receipts.
Universe needs USM storage, WHS ownership and managed-auth integration before it
can enable this feature. Final-version pairwise links and collaborator approval
policies must not silently broaden existing owner consent.
