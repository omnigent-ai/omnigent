---
service: omnigent
created: 2026-11-21
---

# Expand tickets and condense supporting notes

## Intent

Give a ticket more reading room when needed and keep long planning and review notes from dominating its detail view. Preserve the board, drafts, and direct agent chat flow.

## Context

Tickets open in a resizable project side panel. Plan, review, and completion narratives currently render in full; the comment composer stays pinned below them.

## Requirements

- R1: Offer desktop expand/restore controls in the ticket header, including new tickets. Expansion fills the project content area without remounting the editor.
- R2: Restore the prior side-panel width and board when leaving expanded view. Escape restores first, then closes; nested overlays retain their Escape handling.
- R3: Show plan, review, and completion notes as compact, keyboard-accessible disclosures, collapsed by default with a one-line preview.
- R4: Keep review actions visible while notes are collapsed. Editing a plan opens it, and collapsing preserves its draft.
- R5: Preserve ticket/comment drafts, agent response routing, chat return navigation, Sources controls, and mobile behavior.

## Clarifications

- Q1: Does full screen mean the browser Fullscreen API?
  A1: No. The ticket fills the project's content area, keeping app navigation available. Phones already use the full content width.
- Q2: Do these controls change ticket data?
  A2: No. Expansion and disclosure state are local UI state; existing explicit saves and review actions are unchanged.

## Acceptance Criteria

- AC1: GIVEN an open desktop ticket WHEN it is expanded and restored THEN the board hides and returns, the prior panel width returns, and unsaved fields survive.
- AC2: GIVEN an expanded ticket WHEN Escape is pressed without a nested overlay THEN it restores without closing; a second Escape closes it.
- AC3: GIVEN a ticket with planning, review, or completion notes WHEN opened THEN each narrative is collapsed with a preview, and its trigger reveals the full Markdown.
- AC4: GIVEN a collapsed plan WHEN Edit is chosen THEN the editor opens; collapsing and reopening retains its draft, and Save uses the existing update action.
- AC5: GIVEN a review ticket with collapsed notes WHEN Approve or Request changes is chosen THEN the existing confirmation/feedback flow remains available and unchanged.
- AC6: GIVEN an expanded ticket with drafts WHEN opening an agent chat and returning THEN its expansion and drafts remain; closing or switching tickets resets expansion.
- AC7: GIVEN a phone viewport WHEN opening a ticket THEN it fills the available width without a redundant expansion control.

## Constraints

Use existing UI primitives, icons, Markdown preview helpers, and design tokens. Keep the mounted ticket and board stable across layout changes. Work only in the isolated Sources worktree; do not alter shared servers, live records, or another agent's files.

## Out of Scope

Browser fullscreen, a new ticket route, persisted layout preferences, backend/schema changes, automated approvals, and changes to Sources or agent orchestration.

## Plan

Use page-local expansion state and a header toggle, mirroring workflow planning chat. Reuse the shared Collapsible primitive and Markdown preview helper for compact narrative rows; review actions remain outside collapsed content. Extend the existing page and ticket component scenarios, with mocked data only.

Affected components: `ProjectHomePage`, `ProjectTicketPanel`, and their colocated tests. No data model or API changes. Main risks are remounting drafts, hiding review actions, and interfering with Escape handling.

## Tasks

- [x] T1: Add in-place ticket expansion and restoration (AC1, AC2, AC6, AC7).
- [x] T2: Add compact narrative disclosures with safe plan editing and visible review actions (AC3–AC5).
- [x] T-val: Run acceptance criteria verification through focused non-browser component tests and provide manual preview steps.

Verification: 40 tests passed across `ProjectHomePage.test.tsx` (AC1, AC2, AC6, AC7) and `ProjectTickets.test.tsx` (AC1, AC3–AC6), including failed-save recovery. No browser verification was performed for this iteration.

Manual check: open a ticket, enter an unsaved comment, choose **Expand ticket**, and restore it. The comment remains and the board returns. Open Plan or Ready for review to read the full notes; collapse them again and confirm review actions remain available. Escape restores an expanded ticket before closing it.
