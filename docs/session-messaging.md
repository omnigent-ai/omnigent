# Owner-controlled session messaging

An owner can enable communication between all sessions they own in one tenant
workspace, including shared sessions and sessions created later. Accounts/OIDC
authentication is required in this candidate. Header/proxy auth does not yet
distinguish owner credentials from runner credentials. The personal
setting defaults to Off. The deployment feature `session_messaging` also defaults
to disabled; this implementation does not activate it globally.

## User journey

1. Open a session → **Session communication** → **Allow communication**.
2. Ask its agent to send work to another owned session. The SDK or Codex tool is:

   ```json
   {"name":"sys_session_message","arguments":{"session_id":"<worker-id>","message":"Check the build and send back the result."}}
   ```

3. The worker receives an ordinary input with its source session identified.
   Its queue/steering behavior and structural parent stay unchanged. Only an
   explicit `sys_session_message` back to the manager copies a reply.
4. Canvas arrows show recent communication between visible sessions. Open an
   arrow to inspect its direction. Activity does not grant permission.
5. Turn communication Off to reject subsequent sends, including replies.

**Queue decision pending:** the current candidate preserves normal processing of
already admitted input. Confirm this boundary before landing; stronger cancellation
would need additional lifecycle work. Existing message copies remain readable.
The final permission check runs after runner readiness. Requests already past that
check may finish during a concurrent setting change; Off is not a queue-drain barrier.

## Permission boundary

The server verifies the source runner and a signed envelope for the inputs that
its runtime consumed. It rechecks the original requester's current ownership of
both sessions and their personal setting on every send. A shared editor cannot
borrow the owner's authority, even if the editor can access both sessions.
A → B → C preserves the original requester. Mixed-user turns, missing authority,
forged envelopes, wrong runners/workspaces and chains beyond eight hops fail closed.

Owner UI/CLI credentials can change the preference, including an agent given
those credentials. Ordinary runner credentials cannot. This depends on the
credential-purpose prerequisite in PR #11135. Existing discovery, history and
child-management permissions are unchanged. The bound runner is trusted to attest
consumption; the feature is not a sandbox against a compromised runner or a
process holding the owner's UI/CLI credentials.

Signed input metadata contains its version, input/origin IDs, requester/account
generation, workspace, destination/runner binding and hop count. It travels outside
model arguments and prompt text. Correlation does not grant authority. Native
Codex client IDs are checked against actual consumed-input RPC receipts; copied
transcripts and display attribution are never authority.

## Storage and compatibility

No schema migration or task service is added. One protected record in the existing
preferences table stores the switch and its version. Existing conversation resource
events store signed, content-free activity; each Canvas query reads at most 100
recent resource events and filters both endpoints by viewer access. Older activity
can age out of that view. There is no complete communication ledger or task polling.

Existing event delivery reports acceptance/failure; an ambiguous transport outcome
must not be automatically retried. There is no new exactly-once guarantee. On a
runner restart, restored/imported history cannot authorize new sends: provide fresh
authenticated input. Accounts/OIDC use a purpose-derived signing key, so signed
activity survives server restarts. Header/proxy auth needs credential-purpose and
signing-key integration; whether to include it in v1 remains a product decision.
Universe is a separate near-term integration.

Verified-consumption support is implemented for SDK execution and Codex native.
Other native harnesses do not advertise the new tool. Direct terminal input with
no authenticated input receipt cannot authorize it. Existing child sends retain
automatic completion forwarding; independent messages do not use that mechanism.

## Settings API

Use owner UI/CLI authentication:

- `GET /v1/session-communication/settings` → `{"enabled":false,"version":null}`.
- `PUT /v1/session-communication/settings` with
  `{"enabled":true,"expected_version":null}` returns the new version.
- Send that version on the next update; a stale update returns 409.

`sys_session_message` uses the existing session events endpoint with the internal
`session_message` event type and verified runner context. User-supplied requester
IDs are not accepted as authority. No new delegate/reply/task API is introduced.

See [the verification map](../feature-map/session-messaging.md) for exact journeys.
Directed links, collaborator ACLs, scoped approvals and optional task reliability
remain follow-ups, not prerequisites for this smaller version.
