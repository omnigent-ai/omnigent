---
service: omnigent
created: 2026-11-18
---

# Connect project ticket sources

## Intent

Make it easy to bring work from issue trackers and chat into a project's ticket
board. A project-page source picker opens the existing automation creation dialog
with a useful ticket-intake preset, ready for the user to review.

## Context

The project prototype has a ticket board and an ordinary orchestrator session.
Automation creation already supports Jira, Slack, Linear, GitHub, and PagerDuty
triggers, editable instructions, schedules, execution settings, and a project.
This prototype joins those existing surfaces without adding a sync service.

## Requirements

- R1: Offer a compact, accessible Connect source action on the project ticket board.
- R2: Selecting a provider opens the existing automation dialog with the provider,
  project, automation name, and ticket-intake instructions prefilled.
- R3: Preserve the normal editable source, schedule, permissions, and execution
  controls. Keep existing automation entry points working unchanged.
- R4: Selecting, editing, or canceling a preset performs no project, ticket, or
  automation writes. Only the existing explicit save action creates an automation.
- R5: Keep all changes on a separate branch/worktree based on the ticket prototype;
  do not change active development servers, accounts, or live records.
- R6: Support multiple sources per project, including multiple sources of the same
  provider. Each source is a separate automation; list them together without
  replacing or deduplicating them by provider, and keep adding sources available.

## Clarifications

- Q1: Does selecting a source connect an account or import existing issues?
  A1: No. It selects a provider preset. The existing form handles the user's source
  scope and connection requirements; saving uses normal automation creation.
- Q2: How does the Jira board preset identify its board?
  A2: Use the existing Jira project and optional board JQL controls. Account-backed
  board discovery and board-URL resolution are not part of this prototype.
- Q3: What do the default instructions do?
  A3: Create or update a project ticket for a matching actionable item, retain its
  source URL, avoid duplicates, preserve local status/decisions, and leave execution
  to the project orchestrator. Existing trigger link-back/reply behavior remains
  visible and unchanged; this is not bidirectional status synchronization.
- Q4: Can a project have more than one source?
  A4: Yes. Show the project's existing source-trigger automations using the shared
  automation query. Keep the user on the board after creation so they can add
  another. A new source does not modify the project's other automations.

## Acceptance Criteria

- AC1: GIVEN a ticket board with compatible automation capabilities WHEN the user
  opens Connect source THEN Jira, Slack, Linear, GitHub, and PagerDuty are available.
- AC2: GIVEN a selected provider WHEN the automation modal opens THEN its source,
  project, name, and ticket-intake instructions are prefilled and editable.
- AC3: GIVEN an open preset WHEN it is canceled and a different preset is opened
  THEN the new values replace the canceled draft without creating records.
- AC4: GIVEN a prefilled project WHEN the user explicitly saves THEN the normal
  automation request uses that project's stable ID, or resolves its name only at
  save time if the project does not yet exist.
- AC5: GIVEN an unsupported server WHEN the ticket board is displayed THEN no
  unusable source-creation action is offered.
- AC6: GIVEN an ordinary automation entry point WHEN its dialog opens or edits a
  saved automation THEN it retains its existing defaults and saved values.
- AC7: GIVEN multiple source automations in a project WHEN its board opens THEN
  each is shown independently, including multiple instances of the same provider,
  with a link to its existing automation page and paused/failed state when relevant.
- AC8: GIVEN saved sources WHEN another source is selected or canceled THEN the
  existing sources remain visible and unchanged, and creation keeps the board open.
- AC9: GIVEN a loading or failed source list WHEN the board is displayed THEN the
  state is visible, cached sources are retained, and Connect source remains usable.

## Constraints

- Reuse existing components, source icons, templates, and mutation hooks; add no
  dependencies, database migrations, or backend API changes.
- Keep the automation form lazy-loaded and source selection keyboard accessible.
- Preserve other agents' edits and the shared development database.
- Verify with focused non-browser component tests; do not launch browser checks.

## Out of Scope

- OAuth/account connection work, live automation creation, and external writes.
- New import/sync engines, webhooks, status mappings, or source-of-truth policy.
- Account-backed board/channel discovery and a persistent linked-sources model.
- Ticket lifecycle, related-ticket, lead-session, or navigation changes.

## Plan

### Approach

Mount a small source-picker component above the ticket lanes. Reuse the existing
trigger catalog and lazy automation dialog. Extend the dialog's optional initial
values to seed the project and trigger instructions on a fresh open only.
List the project's source-trigger automations through the existing route-scoped
query, keyed by automation ID rather than provider. Reuse its mutation invalidation
and link to the existing automation detail pages; add no source-association table.

### Affected Components

- `web/src/projectHome/ProjectTicketBoard.tsx` — mount the source affordance.
- `web/src/projectHome/ProjectTicketSources.tsx` — source choices and presets.
- `web/src/components/scheduled/CreateScheduledTaskDialog.tsx` — optional prefill.
- Colocated component tests — source selection, reset, capability gates, and save.

### Data Model Changes

None.

### API Changes

None. Reuse the existing automation creation request.

### Risks

Project names can change while the modal is open; retain the selected project ID.
Prefills must not overwrite user edits or leak into later ordinary dialog opens.
Existing trigger templates require credentials and may post source replies;
do not present a selected preset as an already connected or read-only integration.

## Tasks

### Phase 1 — Prefilled creation

- [x] T1: Add optional project and trigger-instruction seeding to the existing dialog.
- [x] T2: Add the source picker and project ticket-intake presets.
- [x] T3: Mount the picker on the board without changing the shared page header.
- [x] T4: Show multiple saved source automations, retaining add-source access.

### Validation

- [x] T-val: Run focused non-browser acceptance tests and record manual review steps.

AC1–AC3 and AC5 are covered by the source-picker component tests; AC2, AC4, and
AC6 by the shared dialog's prefill/project/trigger/session scenarios. AC7–AC9 are
covered by source-list tests with mocked API responses and cache refreshes.
The focused runs passed 95 tests; the frontend app type check and changed-file lint passed.
No browser or live-provider execution was performed.

For manual review, open a project ticket board, choose Connect source, and select
Jira board or Slack channel. Review the source controls, prefilled name and
Instructions, and Project under Advanced. Cancel, then choose another provider;
the new preset must not retain the canceled draft. Existing source automations
should remain listed independently, including repeated providers. Selecting a
saved source opens its automation page. Saving is a real automation creation
action, so cancel when only reviewing the prototype.
