---
service: omnigent
created: 2026-11-18
---

# Project ticket board

## Intent

Track work as tickets. The project orchestrator is an ordinary session,
automatically created in the project, that works through its board.

## Context

Replace the project's session board with tickets. Reuse the existing project
header, new-session composer, session quick view and side chat, and UI primitives.

## Requirements

- R1: Tickets belong to a project and exist independently of sessions.
- R3: Open, In progress and Done. Title-only creation and reopening are supported.
- R6: Append attributed comments, changes, execution summaries and output events.
  PRs are `{type: "pull_request", url}` outputs, deduplicated for display.
- R7: No ticket, comment or history deletion, including through project deletion.
- R11: The project owner and their same-project agents can work on the board.
- R16: Create one ordinary orchestrator session when creating a project. Open it
  with the existing session pane; use normal session creation and messaging.
- R17: Automatically deliver ticket updates to that session, retaining pending
  delivery across restarts and avoiding notifications for its own writes.
- R19: Tags have a key and optional value. The presence of `needs-human` shows
  Needs human input. A comment explains the request; removing the tag resolves it.
- R20: Done requires a completion summary. A finished session does not finish a ticket.
- R21: Keep New ticket and New session. No search or tag-filter UI.

## Clarifications

- Q5: Keep projects containing tickets rather than deleting their history.
- Q6: Reuse today's private-project permissions; project sharing is not added.
- Q7: Existing projects can create their ordinary orchestrator on first use.

## Acceptance Criteria

- AC1: GIVEN a project WHEN a title-only ticket is saved THEN it appears in Open.
- AC12: GIVEN recorded work WHEN a session is removed THEN ticket history remains.
- AC17: GIVEN an unfinished ticket WHEN its worker ends THEN it remains unfinished.
- AC21: GIVEN a ticket update by someone else WHEN it commits THEN its orchestrator
  receives a durable ordinary session message, without a caller notification option.
- AC22: GIVEN a ticket WHEN `needs-human` is toggled THEN its badge changes without
  changing status, regardless of the tag's value.
- AC23: GIVEN competing edits WHEN they commit THEN the last write wins; each edit
  and its event commit together, without versions or conflict handling in the UI.
- AC24: GIVEN a same-project session WHEN it is explicitly linked THEN the ticket
  shows it without creating or restarting the session.
- AC25: GIVEN duplicate PR outputs WHEN displayed THEN the PR appears once and
  both attributed events remain in history.
- AC26: GIVEN a new project WHEN creation succeeds THEN its normal orchestrator
  session is created and accessible through the existing session chat.
- AC27: GIVEN the board WHEN New session is used THEN the existing composer opens
  and creating a session does not create a ticket.

## Constraints

Use existing permissions, managed SQLAlchemy stores, bounded paginated APIs and
Alembic. Last-writer-wins edits; atomic ticket/event writes. No new agent runtime.
Keep schema changes separately reviewable from the application changes.

## Out of Scope

Priorities, extra statuses, filters, custom workflows, issue-tracker sync, PR
unlinking, project sharing, attachments and specialized agent builders.

## Plan

Tickets store their title, description, status, tags and timestamps. Append-only
events hold attribution and typed payloads, including PRs. Session associations
are separate many-to-many links. A project references its ordinary orchestrator
session. A small durable notification outbox uses the existing session-input path.

Expose project-scoped ticket REST APIs and `sys_ticket_list`, `sys_ticket_get`,
`sys_ticket_create`, `sys_ticket_update`, `sys_ticket_comment`, and
`sys_session_link_ticket`. The web board uses existing controls and styling;
the orchestrator uses `ProjectSessionPanel` / `FullSessionChat` unchanged.

## Tasks

- [x] T4: Add bounded ticket storage, APIs and system tools.
- [x] T20: Auto-create the normal project orchestrator and deliver updates.
- [x] T12: Build the ticket board and details using existing components.
- [x] T-val: Verify permissions, transactions, outputs, notifications and core UI flows.
