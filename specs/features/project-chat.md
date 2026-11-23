---
service: omnigent
created: 2026-11-22
status: in-progress
---

# Board-first projects with an Orchestrator

## Intent

Open projects on the ticket board. Orchestrator opens the project's saved session
in the normal session view, not an embedded project chat.

## Context

The complete Maestro/chat-first version is preserved on
`checkpoint/maestro-project-chat-20261122` at `ce617b545f`. Keep its ordinary-session creation,
`main_session_id` pointer, authorization, retry keys and safe replacement rules.
Orchestrator is a session role, not a special reusable agent definition.

## Requirements

- R1: Projects open on the board. Remove Chat/Board tabs and embedded project chat.
- R2: Resolve the saved session, never the most recent session. Opening it only reads.
- R3: Keep existing tickets and project-defined stages; discussion alone creates no work.
  Any conversational project session may create or reuse sessions but creates no tickets
  unless asked; tickets track persistent, staged work.
- R4: Ticket and agent links open the normal session view. Preserve drafts and ticket context.
- R5: Keep the board beside ticket details, with in-place expansion.
- R6: Preserve confirmed ticket-update links; no new approval or PR actions.
- R7: Reuse normal session controls, permissions, attachments and placement behavior.
- R8: Put Orchestrator, Connect source, New session and New ticket in the project header.
- R9: Agent identity and descriptions always come from the actual bound agent.
- R10: Preserve shared full-session response identity and message grouping.
- R11: Keep bounded ticket queries and confirmed update links to the board.
- R12: Existing Orchestrator opens `/c/<session_id>` without creating or forking.
- R13: Use existing button styles and keep phone actions accessible.
- R14: No extra explanatory copy, canned prompts or substitute composer.
- R15: Keep Edit stages and project rename/delete in the project menu.
- R16: Name newly created orchestrator sessions Orchestrator, not Main.
- R17: Stop seeding Maestro. Existing agent records and conversations survive.
- R18: Preinstall the orchestrator skill and append framework-owned role instructions at startup.
- R19: Preserve the project-wide needs-response count, including its incomplete-count marker.
- R20: Preserve actual-agent icons on message-origin rows.
- R21: Preserve actual-agent attribution and response highlighting in ticket details.
- R22: Preserve deduplicated agent icons on ticket cards.
- R23: Project creation is name/icon, Run on, then Orchestrator. Pick an actual native
  agent, defaulting to the first available one; keep model and effort selections.
- R24: Missing/archived Orchestrator opens the ordinary composer. Submit with a stable
  retry key and expected-pointer comparison; preserve drafts on failures and races.

## Clarifications

- `main_session_id` stays the storage/API name. No membership table or primary label.
- Native agents supply their own harness: Codex means `codex-native`, not SDK Codex.
- Explicit choices are not silently changed. An unavailable host/agent blocks creation.
- The role comes from the project pointer, not the agent name or session title.
- Framework instructions are additive; do not modify the agent's authored prompt.
- Archived/missing pointers stay until successful replacement. Inaccessible sessions
  do not permit replacement; a bare session 404 is not proof of deletion.
- Existing Maestro sessions are not rebound or restarted. Only default Main titles
  may be explicitly renamed; other saved titles are preserved.
- Legacy project-chat URLs resolve to the normal session, never an embedded chat.

## Acceptance Criteria

- AC1: GIVEN a saved Orchestrator WHEN opening it THEN the same session opens without a write.
- AC2: GIVEN a ticket WHEN its stage changes THEN board and detail resolve the same record.
- AC3: GIVEN legacy ticket-chat context WHEN navigating THEN its draft survives in the target session.
- AC4: GIVEN a confirmed update batch WHEN following its link THEN the board shows those tickets.
- AC5: GIVEN an offline session WHEN opening Orchestrator THEN it is reused, not replaced.
- AC6: GIVEN a native agent WHEN viewing its session THEN its actual identity is shown.
- AC7: GIVEN a response block WHEN rendering THEN shared grouping and actions remain intact.
- AC8: GIVEN a missing Orchestrator WHEN composing THEN normal model/effort and placement controls work.
- AC9: GIVEN an existing draft WHEN opening Orchestrator THEN it remains on the same session.
- AC10: GIVEN a ticket project WHEN opening it THEN the board/header actions appear without Chat tabs.
- AC11: GIVEN the project menu WHEN choosing Edit stages THEN the existing editor opens.
- AC12: GIVEN legacy session links or ticket-disabled projects WHEN navigating THEN existing access works.
- AC13: GIVEN a new project WHEN creation succeeds THEN its saved session is named Orchestrator.
- AC14: GIVEN a fresh server WHEN seeding THEN native agents are present and Maestro is not seeded.
- AC15: GIVEN a designated native session WHEN preparing startup THEN its skill and role prompt are present.
- AC16: GIVEN tickets needing a response WHEN viewing the board THEN the bounded project summary appears.
- AC17: GIVEN a peer message WHEN viewing its origin THEN the actual agent icon/name are used.
- AC18: GIVEN ticket-linked sessions WHEN opening their links THEN the normal session view opens.
- AC19: GIVEN multiple sessions of one native agent WHEN viewing a card THEN its icon is deduplicated.
- AC20: GIVEN absent/missing/archived Orchestrator WHEN opening it THEN creation waits for submission;
  unavailable or active sessions never permit silent replacement.
- AC21: GIVEN a failed or conflicting replacement WHEN retrying THEN the key and draft are preserved.
- AC22: GIVEN agent/model/effort and placement WHEN creating a project THEN those choices persist;
  native agents send no harness override and general preferences stay unchanged.
- AC23: GIVEN a conversational project session WHEN the user only chats THEN it answers or
  delegates to sessions and the board gains no ticket.

## Constraints

Preserve authorization, bounded queries, actual-agent identity and existing user data.
No browser verification, model runs, unsolicited worker messages or ticket mutations.
Use only the owned preview; do not operate another preview or publish changes.

## Out of Scope

Project-agent memberships, scoping rules, scheduling changes, live session rebinding,
running-harness migration and another database/API pointer rename.

## Plan

1. Save the complete Maestro version on a signed-off checkpoint branch.
2. Select native agents and rename creation defaults; retire Maestro seeding.
3. Restore board/header actions and replace embedded chat with normal-session navigation.
4. Reuse the ordinary replacement composer, preserving lifecycle checks and drafts.
5. Verify native skill/role delivery and focused creation/navigation scenarios.

### Contract and rollout

- `POST /v1/projects`: config agent_id/model/effort and existing placement fields,
  plus a stable UUID Idempotency-Key. Native agents omit config.harness.
- Replacement uses ordinary `POST /v1/sessions` with project_main_session,
  expected_main_session_id and the retry key. No special creation endpoint.
- Keep additive main_session_id schema-first rollout; legacy pointer reads remain
  compatible through 0.25 and a two-week observation window. No full-table backfill.
- An interruption before pointer assignment can leave an unlaunched candidate; no
  recovery job is included. Running native sessions retain cached startup instructions.
- Native Codex structured ticket-card transport remains unverified where its
  forwarder omits generic MCP call items; never infer writes from assistant prose.

## Tasks

- [x] T1: Preserve Maestro checkpoint with normal hooks and sign-off; no push.
- [x] T2: Native-agent defaults, Orchestrator title and modal controls.
- [x] T3: Board-first header and normal-session routing.
- [x] T4: Safe replacement, legacy links and draft preservation.
- [x] T5: Retired Maestro seeding; native framework skill and role coverage.
- [x] T-val: Focused non-browser checks, preview HTTP checks and manual review steps.

### Verification

- Passed — AC1, AC3–AC5, AC8–AC13, AC16, AC18, AC20–AC22: focused creation,
  resolver, header, board navigation and replacement-composer tests. Covers native
  selection, offline reuse, authoritative missing/archived state, conflicts and drafts.
- Passed — AC13–AC15, AC22: server creation/seeding and runner skill/role tests.
  Covers Codex native launch metadata, unavailable/suppressed defaults and authored-prompt preservation.
- Skipped rerun — AC2, AC6–AC7, AC17, AC19: existing stage/identity/attribution
  scenarios are preserved from the checkpoint; their behavior was not changed here.
- Passed — TypeScript, targeted Python typing/lint, changed-web lint and diff whitespace.
- Passed — owned preview health, host reconnect and 17 page/module HTTP checks.
  Only its backend was reloaded; existing session agents/history were preserved.
- Not run — browser rendering or real model execution. Existing native sessions
  retain cached startup instructions; the role/skill checks use isolated tests.

### Try it

1. Open a project: the board and ticket detail panel should appear without Chat/Board tabs.
2. Click Orchestrator: its saved conversation should open in the normal session view.
3. Create a project: choose Run on, then an Orchestrator agent, model and effort.
   Codex selects the native harness; the new session is titled Orchestrator.
4. For an archived/missing saved session, Orchestrator should show the usual composer
   and create nothing until submission.
