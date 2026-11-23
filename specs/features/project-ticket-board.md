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
- R3: Projects own ordered stages, defaulting to Open → Working → Review → Done.
  The first stage starts new tickets; the last means complete. Rename, reorder,
  add and remove stages without changing their stable IDs. Occupied removals
  require a surviving destination; moves and configuration commit together.
- R6: Append attributed comments, changes, execution summaries and output events.
  PRs are `{type: "pull_request", url}` outputs, deduplicated for display.
- R7: No ticket, comment or history deletion API, including through project deletion.
  The one-time internal-field cleanup in R26 preserves authored content.
- R11: The project owner and their same-project agents can work on the board.
- R16: Create one ordinary orchestrator session when creating a project. Open it
  with the existing session pane; use normal session creation and messaging.
- R17: Automatically deliver ticket updates to that session, retaining pending
  delivery across restarts and avoiding notifications for its own writes.
- R19: Labels have a key and optional value. The presence of `needs-human` shows
  the pink Needs response pill. A comment explains the request; removing the
  label resolves it. This is independent of a session's pending approval prompt.
  The API and ticket tools use `labels: [{key, value?}]` and reject `tags` arguments.
  Existing label values and historical label data remain unchanged; historical field
  names are projected as labels when read.
- R20: Results are ordinary comments, not a dedicated completion field. A finished
  session does not finish a ticket.
- R21: Keep New ticket and New session. No search or label-filter UI.
- R22: Use the description and ordinary comments for approach and acceptance criteria.
  Agents investigate routine choices; ask humans only for necessary decisions
  or authority. Explicit approval gates cannot be skipped.
- R23: Record feedback, evidence, limitations and human checks as ordinary comments.
  Project stages define the workflow; tickets impose no built-in plan or review gate.
- R24: Explicit approvals stay in the responsible agent's chat. Ticket comments
  and stage moves do not answer prompts, imply approval or clear outstanding elicitations.
- R25: Persist symmetric, untyped related-ticket links. Link/unlink is idempotent,
  same-project, and recorded on both tickets; no self-links or implied dependencies.
- R26: Remove `lead_session_id` from API/tool contracts and physical ticket storage.
  Remove its structured history keys and delete only updates containing nothing else.
  Reject old arguments, including null, without a compatibility alias. Preserve ordinary
  session links, involvement, authored text, other history, labels and stage ownership.
- R27: Show one Agents section near the top: the project orchestrator and linked
  sessions, with names, brief involvement, activity and explicit chat links.
  Reuse existing chat with Back to ticket and retained drafts. No phase-dependent
  contact card or duplicate Sessions section.
- R28: Workers record chat decisions, plan changes and approvals back on the
  ticket. The orchestrator follows phases, related work, verification and human
  review without adding a new runtime or treating every discussion as approval.
- R29: Hovering or keyboard-focusing a board ticket highlights its directly
  related visible tickets across lanes, without changing selection or phase.
- R30: Store an optional short involvement description on each ticket/session
  link. Updating it retains attributed history; do not infer roles from phase.
- R31: Show Respond for agents with actual pending prompts, otherwise Open chat.
  A needs-human label alone does not select or highlight an agent; comments explain
  the question and link to the relevant chat. Open the exact session; comments and
  stage moves do not resolve an elicitation or authorize auto-approval.
- R32: The orchestrator must never initiate human elicitations or synchronously
  wait for a worker's human answer. Workers own prompts. The orchestrator records
  the question and worker, stays available for messages and independent work,
  and can ask non-blocking questions in ordinary chat. Never bypass permission gates.

## Clarifications

- Q5: Keep projects containing tickets rather than deleting their history.
- Q6: Reuse today's private-project permissions; project sharing is not added.
- Q7: Existing projects can create their ordinary orchestrator on first use.
- Q8: `needs-human` remains independent of phase; comments/briefs explain the ask.
  Screenshots, demos and reports use existing output links and permitted tooling.

## Acceptance Criteria

- AC1: GIVEN a project WHEN a title-only ticket is saved THEN it appears in its first stage.
- AC12: GIVEN recorded work WHEN a session is removed THEN ticket history remains.
- AC17: GIVEN an unfinished ticket WHEN its worker ends THEN it remains unfinished.
- AC21: GIVEN a ticket update by someone else WHEN it commits THEN its orchestrator
  receives a durable ordinary session message, without a caller notification option.
- AC22: GIVEN a ticket WHEN `needs-human` is toggled THEN its badge changes without
  changing status, regardless of the label's value.
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
- AC29: GIVEN saved ticket notes WHEN upgrading to ordinary content THEN all text
  remains readable as Markdown with original history/attribution intact; missing
  snapshots become System comments once, without notifications or timestamp changes.
- AC30: GIVEN ordinary content and Needs response WHEN moving stages THEN both
  survive without built-in review/completion actions. Removed narrative arguments
  are rejected by the API and tools, not translated through a compatibility layer.
- AC31: GIVEN two same-project tickets WHEN linked from either side THEN each
  lists the other once; unlinking retains both histories and changes neither phase.
- AC32: GIVEN unrelated ownership, projects or tenants WHEN linking tickets or
  sessions THEN access is rejected without partial writes.
- AC33: GIVEN a linked agent WHEN its chat is opened and closed THEN the correct
  existing conversation and Back to ticket are available and ticket drafts survive.
- AC34: GIVEN a handoff WHEN linked-session involvement changes THEN prior links
  and attributed history survive; the orchestrator remains accessible without launching work.
- AC35: GIVEN planning or review WHEN workers follow the skill THEN they respect
  approval gates, record decisions/evidence and do not equate session exit with Done.
- AC36: GIVEN related tickets on the board WHEN one is hovered or focused THEN
  its direct neighbors are highlighted; leaving clears the highlight.
- AC37: GIVEN linked agents WHEN opening a ticket THEN one Agents section shows
  the orchestrator once plus paginated linked sessions and their saved involvement.
- AC38: GIVEN an existing link WHEN involvement changes or is cleared THEN the
  update is attributed and transactional; omitted involvement preserves it.
- AC39: GIVEN needs-human and an agent awaiting input WHEN opening a ticket THEN
  the label shows Needs response and the agent shows Respond with its own chat link,
  without resolving prompts through ticket comments.
- AC40: GIVEN a designated orchestrator WHEN composing native or SDK instructions
  THEN the non-blocking rule is included; ordinary workers do not inherit it.
- AC41: GIVEN needs-human and an idle agent without pending prompts WHEN opening
  the ticket THEN the agent remains Idle with Open chat and no manual response highlight.
- AC42: GIVEN existing response-field values and structured history WHEN upgrading
  THEN the column and keys are absent, unrelated bytes and counters are preserved,
  and interruption/retry does not duplicate or discard meaningful events.

## Constraints

Use existing permissions, managed SQLAlchemy stores, bounded paginated APIs and
Alembic. Last-writer-wins edits; atomic ticket/event writes. No new agent runtime.
Keep schema changes separately reviewable from the application changes.

## Out of Scope

Priorities, filters, configurable transition rules, typed dependencies,
automatic dependency scheduling/rollups, issue-tracker sync, PR unlinking,
project sharing, file uploads and specialized agent builders.
Typed agent roles, automatic interpretation of comment approvals, and automatic
answers/dismissal of existing human prompts are also out of scope.

## Plan

Use project-defined stages, description and ordinary comments, with
canonical-pair related tickets. Preserve retired notes in
readable historical activity; import missing snapshots with stable IDs, truthful
System attribution and skipped notifications. Keep source columns inert, with no
legacy API/tool inputs. Update may append a comment atomically.

Remove the response field from readers and writers before its data migration.
Stop old ticket writers, scrub only its root and changes keys in bounded primary-key
event pages, delete emptied internal-only updates, then drop the column. Preserve
event positions, counters, metadata and authored content; retries are idempotent.
Downgrade restores an empty nullable column, not deleted data. Existing native tool
schemas can remain cached; stale field arguments are rejected, not silently accepted.

Add paginated related-ticket reads and `sys_ticket_link` attach/remove; include
related tickets in `sys_ticket_get`. Reuse existing permissions, output events
and notification outbox. Update the board, shared phase controls and ticket drawer;
keep ticket context in chat navigation. Teach the canonical orchestrator skill
the lifecycle and include the same contract in worker briefs. Verify with focused
entity/store/migration/route/tool and non-browser component tests.

Add bounded nullable involvement to ticket/session links and the existing link
API/tool. Use one named Agents list without separate response-contact metadata. Reuse the
existing session badge and chat surfaces, with pink response pills, Running for
session activity, and Respond only for live pending prompts, otherwise Open chat. Strengthen the shared
orchestrator role instruction and canonical skill; preserve mandatory tool approvals.

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
- [x] T25: Persist and expose ticket/session involvement with preserved history.
- [x] T26: Unify Agents and make response routing and pink attention pills clear.
- [x] T27: Encode mandatory non-blocking orchestration and worker-owned prompts.
- [x] T-val3: Run focused migration/store/route/tool/UI/prompt checks; record limits.
  Backend: 76 passed, one external-database check skipped. Focused UI/API checks:
  51 passed, including response routing, drafts, involvement retries and pagination.
  Skill structure validated. The role rule is instruction-level, not a new
  harness-wide prompt interceptor; live model compliance and browser appearance
  were not exercised. Existing human prompts remain open until answered directly.
  To review, open a ticket, inspect Agents, use Respond to reach the owning chat,
  return with a draft intact, and edit an involvement description.
