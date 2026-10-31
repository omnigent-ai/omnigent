---
service: omnigent
created: 2026-10-31
---

# Group independent sessions as siblings

## Intent

Create independent conversations on shared or separate compute and keep them
in one family in the left sidebar. The family is navigation metadata: each
member has its own conversation, runner, permissions and real subagents.

```text
Coordinator                      sandbox 1
  ╰─ Research                    sandbox 1
  ╰─ Implementation              sandbox 2
```

## Requirements

The first seven items transcribe the whiteboard with the subsequent clarifications.

1. Creating a sibling from any member adds a peer to the same family. Nesting
   stays one level deep. The menu action is **+ Add new agent**.
2. Archiving/deleting the representative promotes the oldest active member;
   it does not stop or delete the other members. Restoring an older member
   restores its position. The original source wins same-second creation ties,
   followed by session ID for deterministic ordering.
   When every member is archived, the oldest retained member represents the family.
3. Pinning the representative pins the family. Pinning another member creates
   an individual shortcut. Family pins survive promotion and include future
   members; unpinning the family preserves individual pins.
4. An indented member cannot move independently or detach from its family.
5. Ordinary **Fork** creates a standalone session and can reuse the source's
   ephemeral host. Neither an existing sibling nor an existing standalone
   session can join another family. Membership is assigned only during creation.
6. The representative moves the whole family between projects, including
   archived members. Menus, drag actions, bulk actions and the server enforce it.
7. Nested presentation for shared viewers is optional. Shared views remain flat.

Additional requirements:

- The action opens the normal new-session composer with source context. It
  offers normal hosts/new sandboxes plus only the clicked member's ephemeral
  host, preselected. The family can span hosts; the picker does not offer every
  ephemeral host used by the family. Desktop and mobile menus use this composer.
- Each sibling starts a fresh conversation. No copied-history option is offered;
  users who want copied history use ordinary Fork.
- Siblings have a curved decorative elbow beside their indentation. They never
  appear in the right Agents panel. Expand/collapse has a separate accessible
  button; individual pinned shortcuts can appear flat outside their family.
- Every sibling starts private, including when the source has readers or the
  deployment defaults to public sharing. Family membership grants no access.
- Owner orchestration allows private and effectively read-only shared sources.
  Another principal's EDIT, MANAGE or OWNER grant blocks orchestration. There
  is no new comment tier. Source restrictions include side-chat ancestry.
- Immediate MCP creation produces a child or sibling. Omitted `placement` keeps
  its existing `child` default; `same_host` and `managed` create siblings. Tools
  expose family metadata and retain creator attribution. Scheduled roots and
  ordinary user-created sessions remain independent.
- Automatic session cleanup must retain compute while any session is bound,
  including stopped/archived sessions, unrelated forks and admitted launches.
  Cleanup must not revoke a replacement generation's credentials.

## Implementation

### Storage and list contracts

Reuse `omnigent.session_family_id` labels with the original source session ID
as the durable family ID. Execution parent/root links remain unchanged.
Server-owned family and per-user family-pin label keys cannot be written through
creation labels, PATCH, generic store label methods or policies. Forks drop them.

A family supports up to 100 retained members, including archived members. This
bounds hydration and family-wide mutations. A persistent preference row keyed by
family ID serializes member admission, project moves and family pins across
replicas, even after the original member is deleted. Existing metadata and
profile-label transactions support both single and split database deployments.
As with existing split-store operations, metadata and history cannot commit as a
single distributed transaction; failures conservatively retain host references.

List pages preserve the existing flat pagination cursor and add complete family
context for owner-visible hits. Every expanded member is independently checked
for ownership. Snapshots and WebSocket list updates carry `{id,
representative_id, pinned, pinned_at}`. Clients deduplicate hydrated rows across pages.
Pinned lists include member context without charging each member a pin slot.
Shared viewers receive neither private family context nor protected labels.

The only schema change is the covering label index
`(workspace_id, key, value, conversation_id)`, in a separate prerequisite PR.
A 100,000-label SQLite measurement reduced the representative lookup from
approximately 6.352 ms to 0.002 ms. No new tables or columns are required.

### Creation and compatibility

The JSON and uploaded-agent create paths accept `sibling_source_id`, validate
ownership/sharing/host eligibility, and assign membership before launching a
runner. They skip public-default grants for siblings. New siblings inherit the
source project; the server chooses membership rather than accepting a family ID.

The composer waits for source metadata before initializing its host selection,
so normal asynchronous defaults cannot replace the source sandbox. Drafts and
picker caches are scoped to the clicked source. Normal model, agent, repository,
profile and workspace controls are reused.

`/v1/info` advertises `sibling_sessions`. The UI hides the action on older
servers. MCP independent creation requires the capability to be explicitly true
and fails without a fallback request. Uncertain creates are never auto-retried.

### Compute lifecycle

Host-row locks serialize admission with teardown. Persisted metadata bindings
count as reservations before a runner starts. Deleting metadata releases bindings
and claims last-user teardown in the same transaction; existing tombstone fields
retain exact resource IDs for provider cleanup and retries. All remaining
bindings count, regardless of session activity or family membership.

Automatic cleanup uses `only_if_unused` and the expected provider/sandbox ID.
Explicit whole-host deletion retains its separate semantics. Failed launch
rollback clears only its own binding/generation. Retained automation hosts also
keep a task ownership reference after their run session is removed.

Recovery uses a renewable per-host preference lease across replicas. Only the
lease holder provisions a replacement; waiters consume the same result. Expired
attempts cannot replace the winner. Failed recovery detaches a generation only
if its credential, heartbeat and offline status still match; a late successful
connection is preserved. Pending teardown retries run even when optional
age-based offline reaping is disabled.

Managed-sandbox worktrees remain until sandbox teardown, including when optional
worktree cleanup is requested, so concurrent admissions cannot lose files.
Persistent-host worktree retention checks include archived sessions and directory
descendants.
Provider expiry, explicit whole-resource deletion and optional offline retention
remain sandbox-level policies. Recovery after actual provider data loss creates
fresh compute; it cannot recover uncommitted files from a destroyed filesystem.

## Acceptance criteria

- A creates B on A's sandbox; B creates C on fresh compute. All three are
  independent roots and C is one level deep, alongside B.
- Adding from C offers only C's ephemeral host in addition to ordinary choices.
- Deleting A leaves B's host, credentials and workspace intact; B can send a
  successful next turn. An archived or unrelated same-host peer also retains it.
- Pin/unpin and representative promotion preserve family and individual intent.
  Moving the representative files every member; an individual move is refused.
- Forking a member onto its current host produces a standalone session.
- Read-only sharing permits owner orchestration; edit-sharing rejects it.
  Siblings remain private on deployments with public defaults. Shared viewers
  cannot discover private peers through lists, snapshots or labels.
- Generic labels cannot attach/detach/reassign families. Concurrent first sibling
  creation yields one family in single and split database configurations.
- Concurrent recovery attempts yield one new sandbox. Stale cleanup cannot
  destroy a replacement generation or revoke its credential.
- Desktop row, right-click and header menus, and mobile long-press/header menus, open
  the same fresh-conversation composer. Reloads retain nesting and project/pin
  membership. Real subagent presentation stays separate.

## Verification

Store tests cover family admission, ordering, project moves, pin intent,
protected labels, retained bindings and exact-generation cleanup. API tests
cover both creation transports, ownership, effective sharing and family hydration.
Runner tests cover MCP capability negotiation and failure without fallback.

`tests/e2e_ui/sessions/test_sibling_sessions.py` exercises the real app, two server
replicas, host daemons and runners with a local sandbox provider and scripted
model. It records creation on shared/fresh compute, source-specific host choices,
representative deletion and a successful survivor turn, plus family actions and
mobile navigation. These recordings prove app behavior, not live cloud-provider
or managed-deployment compatibility.

Relevant existing suites cover managed-host/reaper behavior, orchestration,
sidebar menus/pins/projects, composer defaults and fork host selection. PR Test
Plan records exact commands/results and any unverified provider boundaries.
