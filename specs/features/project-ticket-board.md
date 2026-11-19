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
- R3: Open → Planning → Working → Review → Done. Planning and human review may
  be skipped when justified; phases can move backward. Keep `in_progress` as
  Working's API value. Title-only creation starts in Open.
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
- R22: Persist a concise plan containing the approach and acceptance criteria.
  Agents investigate routine choices; ask humans only for necessary decisions
  or authority. Explicit approval gates cannot be skipped.
- R23: Before Review, verify the work and save a review brief: delivered scope,
  evidence, limitations and exact human checks. Default to human review for
  subjective results; skipping requires an unambiguous, objectively verified
  outcome and recorded reason. Silence is not approval.
- R24: Review offers Approve & mark done and Request changes. Feedback returns
  work to Working with an attributed comment; scope changes may return to Planning.
- R25: Persist symmetric, untyped related-ticket links. Link/unlink is idempotent,
  same-project, and recorded on both tickets; no self-links or implied dependencies.
- R26: An optional `lead_session_id` identifies the linked same-project session
  to contact, including while idle. Handoffs preserve prior session links/history.
- R27: Show the lead near the phase with Discuss plan / Chat with agent; reuse
  existing chat with Back to ticket and retained drafts. Without a lead, offer
  Ask orchestrator. Other linked sessions remain available.
- R28: Workers record chat decisions, plan changes and approvals back on the
  ticket. The orchestrator follows phases, related work, verification and human
  review without adding a new runtime or treating every discussion as approval.
- R29: Hovering or keyboard-focusing a board ticket highlights its directly
  related visible tickets across lanes, without changing selection or phase.

## Clarifications

- Q5: Keep projects containing tickets rather than deleting their history.
- Q6: Reuse today's private-project permissions; project sharing is not added.
- Q7: Existing projects can create their ordinary orchestrator on first use.
- Q8: `needs-human` remains independent of phase; comments/briefs explain the ask.
  Screenshots, demos and reports use existing output links and permitted tooling.

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
- AC28: GIVEN existing tickets WHEN the schema is upgraded THEN their status
  codes, content and history survive; the board exposes all five phases.
- AC29: GIVEN a ticket WHEN its plan or review brief is saved THEN it is readable
  as Markdown on the ticket and the attributed change notifies the orchestrator.
- AC30: GIVEN a ticket WHEN entering Review THEN a review brief is required;
  requesting changes saves feedback and phase together; Done still needs a summary.
- AC31: GIVEN two same-project tickets WHEN linked from either side THEN each
  lists the other once; unlinking retains both histories and changes neither phase.
- AC32: GIVEN unrelated ownership, projects or tenants WHEN linking tickets or
  assigning a lead THEN access is rejected without partial writes.
- AC33: GIVEN a linked lead WHEN its chat is opened and closed THEN the correct
  existing conversation and Back to ticket are available and ticket drafts survive.
- AC34: GIVEN a handoff WHEN another linked session becomes lead THEN prior links
  survive; clearing/unlinking the lead restores Ask orchestrator without launching work.
- AC35: GIVEN planning or review WHEN workers follow the skill THEN they respect
  approval gates, record decisions/evidence and do not equate session exit with Done.
- AC36: GIVEN related tickets on the board WHEN one is hovered or focused THEN
  its direct neighbors are highlighted; leaving clears the highlight.

## Constraints

Use existing permissions, managed SQLAlchemy stores, bounded paginated APIs and
Alembic. Last-writer-wins edits; atomic ticket/event writes. No new agent runtime.
Keep schema changes separately reviewable from the application changes.

## Out of Scope

Priorities, additional phases, filters, custom workflows, typed dependencies,
automatic dependency scheduling/rollups, issue-tracker sync, PR unlinking,
project sharing, file uploads and specialized agent builders.

## Plan

Extend ticket entities/store/API/tools with two status codes, bounded optional
`plan` and `review_summary`, nullable `lead_session_id`, and a canonical-pair
related-ticket table. Preserve existing status codes. Update may append a comment
atomically. Leads must already be linked; unlinking a lead clears its reference.

Add paginated related-ticket reads and `sys_ticket_link` attach/remove; include
related tickets in `sys_ticket_get`. Reuse existing permissions, output events
and notification outbox. Update the board, shared phase controls and ticket drawer;
keep ticket context in chat navigation. Teach the canonical orchestrator skill
the lifecycle and include the same contract in worker briefs. Verify with focused
entity/store/migration/route/tool and non-browser component tests.

## Tasks

- [x] T4: Add bounded ticket storage, APIs and system tools.
- [x] T20: Auto-create the normal project orchestrator and deliver updates.
- [x] T12: Build the ticket board and details using existing components.
- [x] T-val: Verify permissions, transactions, outputs, notifications and core UI flows.
- [x] T22: Add lifecycle, related links and lead-session contracts/storage/API/tools.
- [x] T23: Add phase-aware plan/review UI, related tickets and direct chat/return.
- [x] T24: Update the orchestrator/worker contract for planning and acceptance.
- [x] T-val2: Verify AC28–AC36 with focused non-browser checks; document manual review.
  Entity/store/migration/route/runner checks: 65 passed, one external-database
  check skipped; affected registry/relay and route checks passed. UI: 32 passed.
  Skill structure validated; AC35's live-agent behavior and browser review were
  not exercised. No live migration or server/runner restart performed.
  After coordinated upgrade, link two tickets and check hover/focus highlighting;
  open the planning agent and return with a draft intact; request changes and
  approve a reviewed ticket.
