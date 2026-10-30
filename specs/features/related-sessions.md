# Related sessions (interactive draft)

## Intent

Keep several full agent conversations together under one sidebar entry. Users can
create a session themselves, switch the main conversation, and open another session
beside it while each session retains its own delegated subagents.

## Context

The existing session tree already supplies parent relationships, inherited access,
runner affinity and child discovery. The original conversation remains the root.
Full peers are direct children of that root, identified by a protected server-owned
role, rather than a title convention. No new group table is needed.

This PR is an opt-in interactive draft. It uses existing same-environment execution
and leaves remote placement and expanded orchestration unavailable. Enable only the
server's `related_sessions` release feature to try it; old components do not need
to recognize a new feature or advertise new protocol fields.

## Requirements

- **R1:** Show Main and full peers as equal rows in Agents, under one left-sidebar
  root. Independently collapse the helpers belonging to each full session and
  remember the selection across navigation and reload. Place the collapse chevron
  beside the harness icon on the session row; its tray opens directly below it.
- **R2:** Offer New session in Agents and Add agent in the sidebar action menu.
  Open the existing new-session composer in a popup, prefilled from the source.
  Reuse its harness, model, permissions and attachment controls. Create fresh context in the
  source session's environment; a create from peer B attaches under root A but uses
  B's execution environment. Sending the first message creates the session.
  Canceling leaves the ordinary new-session draft and preferences untouched.
- **R3:** Open peers in the main conversation or as persistent group-scoped side
  tabs. Side tabs have their own transcript, attachments, drafts, queue and model
  controls where supported. Full screen opens the existing complete workspace.
- **R4:** Require owner authority for peer creation; public read-only visibility
  does not prevent its owner from creating a peer. Reject forged role labels and
  unsupported destination overrides before creating any row.
- **R5:** Preserve legacy behavior when the release feature or new request field
  is absent. No required host, runner or sandbox-provider protocol changes.
- **R6:** Group actions clearly describe their scope. Archive All and Delete All
  include the group; individual peer actions stay scoped. Undo restores only rows
  changed by its archive operation. Archived peers remain discoverable.
- **R7:** Reuse sidebar rows: drag peers to reorder, rename on hover, and put
  Open in side panel and lifecycle actions in the row menu. In a peer's main
  conversation, reuse the above-composer identity tray and offer Back to Main.

## Clarifications

- The root is a real, visible Main conversation and retains group lifecycle
  ownership. Full peers are not reparented, adopted or detached.
- Machine placement and session relationships are separate. This draft supports
  the source's existing runner only; it does not treat equal missing host IDs as
  proof that two sessions share an execution environment.
- Existing public read permissions are inherited. This draft does not expand who
  can control sessions, what an agent token can access, or preapproved MCP tool
  scopes.
- Full-session side tabs are group-scoped. Real forked side chats, files, browser
  tabs, terminal selection and session drafts remain scoped to their own session.
- Side tabs reuse existing side-chat controls; goal/plan controls and complete
  main-composer menu parity remain available through Open full screen.
- Group archive retains shared worktrees. With this draft enabled, bulk archive
  also retains worktrees and tells the user instead of offering cleanup.
- Forking continues through existing flows. Related-session fork and new sandbox
  choices remain unavailable in this draft instead of silently changing context.
- The popup shows the source environment and fixes its destination. Other hosts
  need a trusted placement capability before they can be offered; a host name or
  provider label cannot establish permission to control it. New custom-agent
  bundle creation remains available through the ordinary new-session flow.

## Acceptance Criteria

- **AC1 (R1):** GIVEN a root, two full peers and helpers, WHEN Main's Subagents is
  collapsed, THEN both peers remain visible and the collapse survives reload.
- **AC2 (R2, R4):** GIVEN an owned peer B, WHEN New session is submitted, THEN the
  new session is attached to B's root using B's runner and a fresh transcript;
  a reader's equivalent request is denied without persisting a session.
- **AC3 (R3):** GIVEN a full peer opened in a side tab, WHEN navigating to another
  peer in the same group, THEN the tab remains available and its messages/settings
  address its own session. An unrelated group's tabs do not appear.
- **AC4 (R4, R5):** GIVEN an old client, host or runner, WHEN the feature is absent
  or disabled, THEN existing child creation and native subagent UI still work;
  new related-session creation is rejected when disabled.
- **AC5 (R6):** GIVEN an already archived peer and active group members, WHEN the
  group is archived and then undone, THEN only members changed by that archive
  return to active state.
- **AC6 (R5):** GIVEN a source on a sandbox, WHEN a request supplies a different
  external host or requests managed provisioning, THEN creation is rejected;
  existing standalone managed provisioning is unchanged.
- **AC7 (R7):** GIVEN a peer centered in the main conversation, THEN its exact
  name appears above the composer with Back to Main. Dragging its Agents row
  reorders peers without changing their parent; hover reveals rename and menu.

## Constraints

Use optional request/response fields and the existing protected label namespace.
No database migration or changes to host hello, runner registration or provider
interfaces. The server rechecks access and placement rather than trusting picker
state. Keep internal research and demo media out of commits.

## Out of Scope

Cross-environment related-session creation, managed sandbox provisioning for a
related session, new MCP worker discovery/dispatch/completion semantics, expanded
cross-harness copying, and moving existing sessions between groups. Hard delegated
credential confinement is required before enabling cross-environment control; a
capability picker alone does not provide that isolation.

## Plan

1. Add the release gate, owner-checked related create and protected peer role.
2. Reuse the Agents panel and creation dialog for flat peers and grouped helpers.
3. Scope full-session tabs to the root and reuse the existing rich chat composer.
4. Add explicit group lifecycle handling with operation-specific Undo.
5. Run targeted server/component checks and record an isolated browser demo.

## Tasks

- [x] Server create contract, authorization and legacy regression checks.
- [x] Agents grouping, creation dialog and sidebar entry point.
- [x] Group side tabs and scoped composer behavior.
- [x] Group lifecycle and archived-peer discovery.
- [x] Isolated browser evidence and compatibility checks.
