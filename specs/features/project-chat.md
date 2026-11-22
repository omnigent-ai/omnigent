---
service: omnigent
created: 2026-11-22
status: in-progress
---

# Chat-first projects

## Intent

Open a ticket-enabled project in its main conversation, using Maestro by default. Keep
Board beside Chat, with the same tickets, sessions and controls in both views.

## Context

Implement the reviewed project-chat mock using Omnigent's existing components.
The coordinated backend work renames the saved pointer to `main_session_id` and
registers Maestro as a real, changeable agent. There are no project-agent
memberships or primary labels. Never select a session by recency.

## Requirements

- R1: Ticket-enabled projects open to Chat. Board, ticket and session links still
  work. Switching views preserves conversation state and drafts.
- R2: Resolve the saved Main session in one adapter. Opening Chat never creates,
  forks or repairs a session. Only an explicitly absent, missing or archived
  Main shows the existing new-session composer; offline or inaccessible does not.
- R3: Use existing tickets and project-defined stages. Discussion alone does not
  create tickets or dispatch work.
- R4: Ticket references open the existing detail panel. Existing worker chats and
  return-to-ticket navigation remain available. Preserve explicit ticket context
  and drafts when asking the main chat's actual agent.
- R5: Updates do not navigate away from Board. No last-visit summary, host banner,
  project-level working-session count or extra dashboard.
- R6: Ticket creation/update cards only. No PR merge or new approval actions.
- R7: Reuse the real session composer and its model/effort, permissions, host,
  attachments and workspace controls. No substitute configuration dialogs.
  Preserve existing mobile, read-only and disconnected-host behavior.
- R8: Tabs are Chat and Board. Chat has the bound agent's icon in a neutral orb,
  without a repeated name below it. Response headers show that agent's clean name.
- R9: Hover, focus or tap on the identity shows only the bound agent's description.
  No harness, model, effort or role fields. Configuration stays in the composer.
- R10: One agent-name header per existing assistant block with a copy/fork/link
  action set. Preserve message grouping, Worked for folds and footer behavior;
  no extra headers for fold-only rows or system notices.
- R11: Group successful ticket create/update results into bounded cards. Show
  the total and at most three distinct ticket rows. View all opens Board scoped
  to those IDs, with a removable filter. Resolve current authorized ticket data;
  never infer a successful write from assistant prose.
- R12: Open full session goes to `/c/<session_id>` for the same conversation and
  retains its draft. It does not create or fork a session.
- R13: Board-only Connect source and New ticket sit at the right of the tab row,
  using the existing outline and primary button styles. New session is the
  message-plus icon beside project settings and uses the normal creation flow.
- R14: No canned prompts, extra labels, suggestion chips or new configuration knobs.
- R15: A separate vertical three-dot button beside the selected Board tab opens
  Edit stages. Reuse the existing stage editor and capability checks; remove its
  duplicate project-level menu entry. Preserve access to project rename/delete.
- R16: The default session created for a project is named Main. Maestro is the
  agent name, not the session title. Reusing a session preserves its saved title.
- R17: Maestro has a blue creature-and-baton mark consistent with the team
  avatars. Use it in the project identity, response headers and composer trigger.
  Keep the existing harness/model/effort controls and their behavior unchanged.
- R18: A new project opens the existing Main session with its saved
  greeting and normal composer. A missing or deleted session is a separate
  recovery state, not the initial chat experience.
- R19: Board has an Inbox-style count of tickets labeled `needs-human`, across
  stages and independent of loaded board pages or filters. Count each ticket once;
  hide a known zero and never clear the count merely because Board was opened.
  A bounded, incomplete count shows `+`, as Inbox does.
- R20: "From…" message rows show the sender's agent icon. Hover, focus or tap
  shows its clean agent name only in a compact card. Keep the session-title link
  and message folding unchanged; reuse cached metadata instead of fetching per bubble.
- R21: Ticket agent rows use the session's actual agent icon and clean name
  (Maestro, Claude Code, Codex, etc.). Preserve the exact session link, status and
  involvement; fall back safely when agent metadata is unavailable.
- R22: Ticket cards have small circular agent icons overlaid on their bottom
  edge. Deduplicate native identities across sessions and forks; keep distinct
  custom agents. Use the existing icon and compact name-hover styles. Fetch
  authorized metadata with the ticket page, not separately for every card.
- R23: Project creation uses name/icon, Run on, then Chat. Reuse host/workspace
  and agent/harness/model/effort controls; preselect Maestro without changing the
  general new-session preference. Send explicit launch selections.
- R24: Main replacement submits through ordinary session creation with the old
  pointer and a stable idempotency key. Refetch on success or conflict. Preserve
  the draft on failure; do not overwrite a competing Main or silently replace
  an inaccessible session. Project creation also uses a stable retry key.

## Clarifications

- This is application implementation, not a change to the running preview, host,
  model, preferences or real project data.
- Backend/schema/runtime work is owned by the separate Main-chat handoff. This
  UI consumes `main_session_id` and the read-only `main_session_state` result.
- Existing `?orchestrator` links remain readable, but new navigation uses Chat/Board.
- The user-facing name is Maestro. Its default description is: "Maestro leads
  your project like a conductor: organizing work into tickets and coordinating
  other agents to carry it out."
- Identity always follows the actual bound agent. Existing generic coding
  sessions are not relabeled Maestro or rebound to its definition.
- `main_session_state` is absent, missing, archived, active or unavailable.
  A session 404 alone is not proof of deletion: refetch the project state.
  Reads leave stale pointers untouched. Offline sessions remain active.
- Ticket batches come from confirmed structured operation results. Unsupported
  output formats retain their ordinary transcript rendering without invented cards.
- Native Codex ticket-card delivery is not verified end to end: its forwarder
  currently skips generic `mcpToolCall` items. This UI handles normalized results
  already present in history; it does not add the missing runtime transport.
- SDK Codex model discovery uses its own provider catalog and requires updated
  host code. Effort choices use the existing SDK harness-family controls.
  Unsupported catalogs stay unavailable; never substitute native-wrapper models.
- The real composer and stage editor are interactive; the standalone mock's
  display-only controls are not product requirements.

## Acceptance Criteria

- AC1: GIVEN a saved coordinator WHEN opening Chat or returning from Board THEN
  the same session and unsent draft remain and no creation request runs.
- AC2: GIVEN a referenced ticket WHEN its stage changes THEN its chat card and
  board/detail views resolve the same current record.
- AC3: GIVEN a ticket and a draft WHEN asking Maestro THEN the exact ticket
  context is retained without replacing the draft or creating another coordinator.
- AC4: GIVEN more than three affected tickets WHEN viewing their update THEN the
  card has three rows and a total; View all scopes Board and clearing restores it.
- AC5: GIVEN an offline or read-only session WHEN viewing Chat THEN existing
  permissions, readable history and draft behavior remain; no host move is implied.
- AC6: GIVEN project Chat WHEN inspecting its orb THEN only the actual agent's
  description appears, with no configuration or role fields or forced Maestro identity.
- AC7: GIVEN multiple outputs and a work fold WHEN rendering one response block
  THEN there is one name header and the existing action set/folding stays intact.
- AC8: GIVEN project Chat WHEN opening composer controls THEN the ordinary session
  controls are used, including the existing model dropdown and Add menu.
- AC9: GIVEN a draft WHEN opening the full session and returning THEN the exact
  session, messages and draft are preserved.
- AC10: GIVEN either project tab WHEN inspecting actions THEN ticket actions are
  Board-only, New session stays by settings, and no canned prompts appear.
- AC11: GIVEN Board WHEN opening its vertical menu and choosing Edit stages THEN
  the existing editor opens; keyboard dismissal and focus return work.
- AC12: GIVEN ticket-disabled projects or legacy deep links WHEN navigating THEN
  the existing sessions board and ticket/session access continue to work.
- AC13: GIVEN a project without a coordinator WHEN creating its default session
  THEN its title is Main; opening an existing session does not rename it.
- AC14: GIVEN Maestro runs on any harness WHEN viewing its orb, responses or
  composer THEN its character mark is used; only responses repeat its name.
  The composer still configures the same session's harness/model/effort.
- AC15: GIVEN a newly created project WHEN opening Chat THEN the saved greeting
  is already in the conversation; there is no extra Start action.
- AC16: GIVEN tickets needing a response WHEN viewing Chat or Board THEN Board's
  badge uses the project-wide summary. Removing the label updates the count;
  a known zero hides it. An incomplete count is explicitly marked, and older
  servers without the summary capability do not receive unsupported requests.
- AC17: GIVEN a message from another session WHEN viewing its origin THEN its
  known agent icon appears and the hover card contains only the sanitized name;
  the existing session link and expand/collapse actions still work.
- AC18: GIVEN linked ticket sessions WHEN viewing Agents THEN each known agent
  has its own icon and sanitized name, including Maestro's dedicated mark, and
  opening its chat still targets the same session.
- AC19: GIVEN several linked sessions using the same native agent WHEN viewing
  their ticket card THEN one circular icon represents that agent, distinct
  agents remain visible and each icon's hover shows only its clean name.
- AC20: GIVEN absent, missing or archived Main WHEN opening Chat THEN the normal
  composer appears with changeable Maestro preselected and no write runs; an
  active offline or unavailable Main does not show a replacement composer.
- AC21: GIVEN Main replacement WHEN a request fails or races another submission
  THEN retries keep the operation key, the draft survives, and a conflict
  refreshes the authoritative pointer without creating another main chat.
- AC22: GIVEN project creation WHEN changing agent/harness/model/effort and
  placement THEN those choices reach the project request and retrying retains
  the same operation key without changing general session preferences.

## Constraints

Reuse routing, design tokens, buttons, menus, popovers, authorization and query
caches. Keep queries bounded. No copied mock assets, sample model names or scripted
work in product code. Preserve other agents' changes. No browser verification,
service operations, live model calls or real ticket mutations for testing.

## Out of Scope

Project-agent memberships, new runtime or scheduling behavior,
new permission modes, PR actions, new sharing rules and preview activation.

## Plan

1. Add Chat/Board navigation and move the existing actions. Preserve deep links,
   the non-ticket sessions board, ticket expansion and project management actions.
2. Isolate Main resolution and embed the shared full-session chat. Keep its
   composer, permissions, drafts and full-session link; add the agent orb using
   existing display-name, icon and session-agent description helpers.
3. Add agent headers at the current response/action boundary. Extend the shared
   chat rendering only through optional project presentation/context hooks.
4. Render confirmed ticket operations as grouped cards. Reuse ticket queries,
   stage badges, detail links and Board with a scoped, removable ID filter.
5. Extend nearby component/unit tests for navigation, session reuse/drafts,
   identity, response boundaries, batch grouping and menu placement. Run focused
   non-browser checks and document any integration or visual-review limits.
6. Set Main as the default project-session title in the existing creation path.
   Do not migrate or rename saved sessions.
7. Use a dedicated Maestro mark and align the mock's new-project scenario with
   the server-seeded greeting. Keep missing-session recovery separate.
8. Add an owner-authorized, bounded ticket summary and reuse Inbox's badge styling
   on the Board tab. Encrypted labels are read server-side, not scanned by the UI.
9. Share agent identity resolution between message origins and ticket agent rows.
   Use existing session metadata, native-agent names/icons and hover-card styling.
10. Add circular ticket overlays from bounded authorized ticket metadata and
    deduplicate canonical agent identities before rendering.
11. Integrate Main state and ordinary replacement creation with the existing
    new-session composer. Retain drafts and retry keys; require HTTP confirmation
    of the pointer assignment before accepting an optimistic pushed session.
12. Reuse placement and launch controls in project creation; keep Host above Chat.

### Contract and rollout

- Create a project with `POST /v1/projects`, explicit launch choices in `config`,
  and a stable UUID `Idempotency-Key`. Native agents omit `config.harness`.
- Replace Main through ordinary `POST /v1/sessions` with `project_main_session`,
  `expected_main_session_id` and the same retry-key behavior. JSON and registered-
  agent multipart support harness overrides; uploaded bundles own their harness.
- On a conflict, refetch Main and preserve unsent input in its composer, merging
  with any existing draft. Do not send it automatically. An unavailable target
  keeps the replacement draft. Connected hosts use the existing first-message
  handoff; managed initial input and attachments remain server-owned.
- Deploy the additive `main_session_id` schema first, in a separate schema change.
  Read the legacy pointer only as a fallback; write only the canonical field.
  No full-table backfill, existing-chat rebinding or mixed old/new Main writers.
  Retain legacy fields through 0.25 and a two-week observation window.
- An interruption between session persistence and pointer assignment can leave an
  unlaunched candidate. The same key returns a conflict; no recovery job is included.
  Assigning an already-running native chat does not refresh its cached startup
  instructions or restart it.

## Tasks

- [x] T1: Project tabs, compact actions, Board menu and compatible routes.
- [x] T2: Shared project Chat, saved-session resolution, identity and draft/context reuse.
- [x] T3: Agent-name response headers without changing grouping or folds.
- [x] T4: Confirmed ticket update cards, current data and scoped Board navigation.
- [x] T5: Main as the default project-session title, independent of agent identity.
- [x] T6: Project-wide response counter, capability gate and focused coverage.
- [x] T7: Sender hover icons and ticket agent identities with focused coverage.
- [x] T8: Deduplicated circular ticket-card overlays and focused coverage.
- [x] T9: Canonical Main lookup, replacement composer, conflict/retry handling.
- [x] T10: Project creation placement/Chat controls and explicit launch selection.
- [x] T-val: Finish integration checks and manual review instructions.

Local verification: 500 focused frontend tests pass across the affected project,
chat, composer and API-client checks. TypeScript and lint of changed frontend files
pass. Coverage includes real Main refetch transitions, preserved text/files/mentions,
native versus SDK choices, bundle metadata, managed first input and retry keys.
The backend owner reports 240 passing checks and one external-database-only skip.
Launch checks use persisted records and mocked launch boundaries, not a running model.

No browser, live model, real ticket, migration or deployment verification was
performed. The implementation is local and uncommitted; preview/services are unchanged.

Manual review after an authorized rollout:

1. Create a test project: name/icon, Run on, then Chat. Confirm Maestro is changeable
   and the selected host, agent, model and effort reach the new Main session.
2. Open Chat, hover the orb, inspect a response header and the ordinary composer.
   Open full session: it should be the same Main with the same unsent draft.
3. Switch to Board. Check the response count, deduplicated agent icons, name-only
   hovers and the vertical Edit stages menu. Ticket-update batches open the same tickets.
4. In a disposable project, archive Main and reopen Chat: no new session should
   exist until submission. Unsent input should survive a failed or conflicting create.
