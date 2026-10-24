# Local RPG office application

Interview specification, 2026-10-24. This document records agreed product
behavior and proposed implementation boundaries; it does not claim that the
application or runtime capabilities have been implemented.

## Scope and presentation

A separate, locally served browser app connects to an already running
Omnigent instance. Existing Omnigent projects are office rooms. The first
release supports SDLC and unthemed projects. Do not start another Omnigent
instance as part of normal application startup.

Use Agent Town as visual inspiration: https://github.com/geezerrrr/agent-town.
Top-down pixel-art interiors support both keyboard walking and direct clicks.
The building is a pixel-art cutaway with an elevator and up to three room
previews per floor. Entering expands a room; exiting returns to the building.
Floor zero is the lobby and reserves a new-room entry. Project creation and
other themes follow the initial existing-project implementation.

The SDLC human avatar is a fixed Product Owner and cannot switch roles inside
the room. Workers have appearances matching their roles. Completed workers
remain at their desks. Clicking a worker or its side-panel button opens its
activity and messaging interface. Display available progress summaries, tool
activity, and stated next steps; do not imply access to private reasoning.

## Agents and theme branches

An unthemed project shows its native harness as one generalist specialist.
An assigned theme has one custom coordinator agent containing the theme,
roles, and workflow. SDLC uses the PM as coordinator. Architect, Developer,
Reviewing Developer, QA, and DevOps have persistent role conversations;
parallel tasks may create additional temporary workers. The human occupies the
PO role.

Theme switching creates or resumes a distinct theme branch within the same
project, conceptually similar to a fork. Preserve previous theme instructions,
workers, sessions, and outputs. Returning resumes that branch, carrying over
relevant context from the outgoing branch. Unfinished work in outgoing branches
continues in the background. Context transfer should be an explicit, traceable
handoff rather than silently rewriting previous conversation history.

The runtime must distinguish project, theme branch, role, session, and task
identity. Work from an inactive branch must retain its original instructions.

Proposed branch isolation (design review). Separate sessions do not isolate
files, Git state, processes, or external effects, so a theme branch is a
workflow workspace with:

- A pinned theme-definition revision, its own task graph, role-session links,
  and artifact references.
- An isolated checkout (for example a Git worktree) for code work, with an
  explicit integration step back to the shared project. Shared external
  systems such as deployments have a single writer branch at a time.
- A lifecycle of active, background, paused, and archived. Background
  execution is a deliberate branch policy with pause, cancel, concurrency, and
  cost limits; the same limits apply to temporary workers.

Returning to a branch reopens its preserved state. Importing context from the
outgoing branch is a separate recorded operation listing its source revisions,
contents, and destination; it requires human approval only when it changes
scope or authority. Assigning a theme to an unthemed project creates the first
theme branch; the generalist session remains reachable as its own branch.
Persistent role identity does not require one ever-growing conversation: a role
may roll over to a new session through a recorded handoff. Background theme
branches ship only after workspace isolation is proven.

## SDLC execution contract

R = responsible, A = accountable, C = consulted, I = informed.
Every task has at least one R and exactly one A. Human approval overrides
automated workflow decisions; it satisfies a workflow gate but never grants
runtime permissions or credentials (see Approvals). The user's supplied matrix
is authoritative.

| Task | PM | PO | Architect | Developer | Reviewing Developer | QA | DevOps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Gather and document requirements | C | RA | C | I | I | I | I |
| Define scope and timeline | RA | C | C | I | I | I | I |
| Define architecture and stack | I | I | RA | C | I | I | C |
| Design APIs and database | I | I | A | R | I | I | I |
| Write code and unit tests | I | I | C | RA | I | I | I |
| Peer code review | I | I | R | C | RA | I | I |
| Create test plans and cases | I | C | I | I | I | RA | I |
| Execute tests and report bugs | I | I | I | C | I | RA | I |
| Fix bugs | I | I | I | RA | I | C | I |
| Set up CI/CD | I | I | C | I | I | I | RA |
| Production deployment | C | I | C | I | I | I | RA |
| Monitor system health | I | I | C | I | I | I | RA |
| Triage and resolve outages | C | I | C | RA | I | C | R |

The requirements board supports editable requirements and acceptance criteria.
The PM can conduct a one-question-at-a-time interview and propose board updates.
Microphone dictation produces an editable transcript before submission.

Direct human instructions are followed unless they affect other roles; those
changes go through the coordinator for scope coordination. Proposed refinement:
workers may acknowledge and investigate immediately, but changes to shared
scope, interfaces, sequencing, or commitments require a recorded coordinator
decision. Human stop instructions take effect immediately without coordinator
routing.

Proposed task contract (design review). The matrix assigns roles; it does not
define deliverables or completion. Each workflow task records:

- Deliverable and acceptance criteria.
- Responsible and accountable identities.
- Dependencies and resource ownership.
- Required review or human gate.
- Evidence required for completion.

Consultation and informing are configured per workflow rather than hard-coded.
Recommended default: deliver Informed updates through an event feed rather than
messages in every role's conversation, to limit token cost and context
pollution. Where the PO is responsible, the task blocks on the human; the PM
may draft but only the PO accepts.

Matrix clarifications (decided by the user):

- Peer code review: a separate Reviewing Developer is responsible and
  accountable, so no developer reviews their own code. The authoring Developer
  is consulted and the Architect remains responsible. A merge with unresolved
  reviewer objections routes to the human merge gate.
- Triage and resolve outages: when the outage triage gate is enabled (off by
  default), the human reviews the outage first, then assigns it back to the
  coordinator with a comment. The coordinator dispatches the work according to
  the matrix row. When disabled, the coordinator dispatches directly.

Default configurable human gates: requirements, architecture, merge, deployment.
Outage triage is also a configurable human gate but is off by default; when
enabled, the human reviews first, then the coordinator dispatches work.
Coding and testing proceed within approved scope. Waiting blocks dependent
tasks only. Work continues with the browser closed while the required runtime
and host remain available.

## Approvals

The building flags rooms requiring human attention. Enter the room to see
requests under its title. Selecting a request opens a dialog containing the
action or artifact, supporting detail, and controls to approve or give feedback.
Do not offer approval directly from the building. Pending requests from inactive
theme branches must remain discoverable with their originating theme identified.

Proposed correctness requirements: persist requests and decisions outside the
browser; bind decisions to an artifact/action revision; reject stale decisions;
make retries idempotent. Enforce gates at the execution boundary, not merely
through coordinator prompts or disabled UI buttons.

Proposed authority model (design review). Workflow approval and runtime
authorization are separate:

- A workflow approval satisfies a named workflow gate.
- Existing Omnigent runtime policies remain authoritative over whether an
  action may execute. Execution requires both where both apply; approving
  deployment never grants credentials or bypasses a runtime restriction.
- Enforcement must mediate every supported execution path for the gated
  operation, including shell commands and alternate harness mechanisms.

Each request records its branch, task, requested operation, target
environment, artifact digest, and gate-policy revision. Define who may decide
and the outcome of rejection, cancellation, expiry, and supersession. The first
release supports a few explicit gate types with explicit invalidation rules;
for example, a requirements change marks a dependent architecture approval
stale. Avoid a generic workflow-policy language.

Pending requests appear in a building-wide attention list and, while the
browser is closed, through notifications. Both link into the room; approval
still happens only in the room's dialog.

## Building ranking

Priorities are Normal, High, Critical; projects default to Normal. Explicit
priority overrides all activity ranking. High and Critical projects never enter
the basement.

### Meaningful interactions

Meaningful interactions are human messages to any session in the project and
submitted approval decisions, including requests for changes. Aggregate them
across all theme branches. Agent-to-agent messages, room visits, and room time
do not count. Identify events by stable source IDs (session and message ID, or
approval-decision ID) and their occurrence timestamps. Deduplicate historical
and live events; retries and reconnects never create activity or reset
deadlines.

### Ordering

Order projects by:

1. Priority: Critical, then High, then Normal.
2. Latest meaningful interaction, newest first. Projects without a known
   interaction follow projects with one.
3. Project creation time, newest first.
4. Project ID, ascending.

There is no activity score, frequency count, decay, or counting window.

### Basement and Keep upstairs

A Normal project enters the basement after 30 × 24 hours of server time
without a meaningful interaction. Measure inactivity from the latest of its
last known interaction, its creation, and, when its history is incomplete, the
app's first successful synchronization. Calendar months and timezones do not
affect eligibility.

A Normal project is not moved down while it has a pending human request or
while **Keep upstairs** is enabled. Keep upstairs affects only basement
eligibility, not rank. Pending requests do not count as interactions.

A new meaningful interaction, raising priority to High or Critical, or enabling
Keep upstairs makes a basement project eligible to return at the next placement
update. A new interaction starts a fresh 30-day period.

### Missing history

Persist each project's first synchronization time; reconnects do not reset it.
Never treat missing history as zero activity. Older history discovered later
updates activity at the next placement update but never shortens the grace
period.

### Recalculation and movement

Update activity and eligibility as events arrive and check inactivity deadlines
at least hourly. Apply placement changes only when the building is first
opened, when returning from a room, when the user selects **Refresh
placement**, and immediately after the user changes priority or Keep upstairs.
Otherwise preserve room positions and show **Placement updates available**.
Attention indicators update immediately. Never interrupt an open room; animate
movement only in the building.

### Floors

Floor zero is the lobby. Each above-ground and basement floor holds at most
three rooms in a consistent left-to-right order. Pack above-ground projects in
rank order with the highest-ranked on the highest floor; floors may mix
priorities. Order basement projects by the same rules, highest-ranked on B1
nearest the lobby. Provide searchable project navigation and a building-wide
attention list covering all rooms, including basement rooms and inactive theme
branches.

### Explain rank

Show priority, position within that priority, latest meaningful interaction,
the reason for upstairs or basement placement, the basement deadline, and
whether a newer placement is waiting. Examples:

> **High · #2 of 5 High projects.** Last human interaction: today at 10:42.
> High projects always remain upstairs.

> **Normal · Upstairs.** Last human interaction: 12 days ago. Basement
> eligibility begins in 18 days unless Keep upstairs is enabled.

> **Normal · Basement.** No human interaction for 41 days. Send a message or
> enable Keep upstairs to return upstairs.

For incomplete history, state the known coverage and grace deadline. For new
projects without interactions, state that inactivity is measured from
creation. The accepted trade-off is that one recent message can move a project
above a more frequently used project of equal priority.

## Future themes

Retain the user-supplied Markdown as the source for future theme definitions;
the summaries here do not replace their full RACI tables.

| Theme | Human avatar | Agent roles |
| --- | --- | --- |
| Research & Archive | Professor | Lead Researcher, Archivist, Field Analyst, Peer Reviewer |
| ER | Doctor | Attending Doctor, Resident, Nurse, Intern |
| Ranger & Recon | General | Commander, Scout, Ranger, Cartographer |

Recon is read/search only, with no community posts or DMs. New Business
Development and Custom remain future lobby choices; their workflows are not
yet specified. In ER, human Doctor and agent Attending Doctor are separate
identities. Preserve the matrix's accountability assignments when implementing.

## Proposed architecture and repository reuse

Use a separate React/TypeScript frontend and a local service adapter. Keep game
rendering separate from accessible DOM panels, dialogs, boards, and navigation.
Select the renderer after a small movement, hit-testing, and sprite prototype;
Phaser is a candidate, not an approved dependency decision.

Existing code inspected during discovery:

- `omnigent/server/routes/projects.py`: first-class project CRUD.
- `web/src/lib/projectsApi.ts`: project membership semantics and default config.
- `web/src/lib/agentsApi.ts`: custom-agent bundle installation and configuration.
- `web/src/shell/SubagentsPanel.tsx`: existing child-session presentation.
- `examples/extensions/hello-page/README.md`: embedded extension pattern; the
  requested app is separate, so this is not its delivery mechanism.
- `sdks/ui/README.md`: terminal components, not a browser SDK.

These files establish reuse candidates, not complete API compatibility. Audit
session messaging, event streaming, history pagination, authentication, forks,
child-session addressing, approval capabilities, and host lifecycle before
implementation. Existing project config stores client defaults; it is not an
execution scheduler.

Persist workflow, approval, role-session, branch, and activity state in a service
that survives browser closure. Reuse Omnigent execution and identity boundaries.
If native sub-agents cannot be resumed or directly messaged, use supported
persistent sessions with explicit role links rather than claiming nonexistent
native capabilities. Any adapter must preserve existing approval enforcement.

### Proposed state ownership (design review)

Record a state-ownership decision before building the office. Each state
transition has exactly one authoritative owner and a reconciliation protocol:

- **Omnigent** owns execution state where practical: approval requests and
  decisions, gate enforcement, durable task transitions, and project, branch,
  role, and session identity. Prefer extending Omnigent over emulating missing
  runtime capabilities in the adapter.
- **The office service** owns presentation state: floor placement, Keep
  upstairs, first-synchronization times, and layout. Project priority may live
  in project config.
- If workflow state must live in the office service, specify it as a durable
  orchestration service: who starts and supervises it while the browser is
  closed (without starting another Omnigent instance), restart recovery, and
  reconciliation with Omnigent.

Store canonical artifacts such as the requirements board where agents read
them (the repository or Omnigent), so approvals bind to a content hash or
commit rather than a browser copy.

The capability audit produces a per-harness support matrix covering resume,
direct messaging, cancellation, approvals, event replay with stable event IDs,
and background execution. Each unsupported behavior has a visible product
fallback.

Rooms show honest worker states: working, waiting on a human, waiting on a
dependency, failed, offline, and completed. Harness crashes, host loss, runtime
restarts, and expired authentication must not look like progress.

Local security: bind the app and service to localhost, check request origins,
and keep Omnigent credentials out of rendering code. State whether voice
transcription runs locally or in the cloud and its privacy consequence. Every
in-game action has an equivalent accessible DOM path.

## Implementation sequence

Proposed sequence (design review): prove execution correctness early and judge
each milestone by whether the office shows who is working, what is blocked, and
where the human's decision is needed.

1. Audit API capabilities and produce the harness support matrix. Record the
   state-ownership decision. Establish a separate local app connection using
   existing authentication, event replay, reconciliation, and no secret
   exposure to rendering code.
2. In parallel:
   - A thin office over existing data: real projects as rooms, real sessions
     as workers with honest states, side-panel messaging, searchable project
     list, and simple priority-then-recency placement.
   - A headless workflow slice: one coordinator, one specialist task, and one
     runtime-enforced durable approval that survive browser closure and
     service restart.
3. Bring the slice into rooms: attention flags, attention list, notifications,
   and the approval dialog.
4. Add the full SDLC roles, task contracts, coordinator configuration,
   requirements board, and PM interviews.
5. Add editable voice transcripts, richer movement and animation, and the full
   ranking and basement rules.
6. Add theme branches once workspace isolation and concurrent execution are
   proven. Reserve branch identity in the data model from step 1.
7. Verify reconnect, background execution, theme switching, concurrent
   decisions, and all supported interaction entry points in an isolated
   environment.

Read the repository contributor and test guides before adding setup or tests.
Use existing primitives and extend compatible scenarios. Do not start or drive
the user's existing instance for verification without following the feature-map
isolation workflow.

## Acceptance walkthrough

Connect to an existing test instance and display its real projects. Set one
project High, enter it by clicking, and return through the elevator. Enter by
keyboard, assign SDLC, edit requirements, and conduct a PM interview. Dictate a
requirement, edit its transcript, then send. Observe persistent specialist
workers, message one through both entry points, and inspect a completed worker.
Trigger an approval, verify its building flag, enter, review the dialog, and
provide feedback before approving the revised request. Confirm independent
tasks continue. Close the browser and reopen to confirm ongoing execution and
durable state. Check 30-day inactivity basement placement, Keep upstairs,
pending-request and priority exemptions, and floor rearrangement only at
placement-update moments without interrupting an open room.

Also exercise failure cases: duplicate and replayed events, stale approvals
after an upstream change, simultaneous decisions, coordinator failure, service
restart during execution, host loss shown as offline, and conflicting edits.
Confirm a stop instruction halts work immediately and that an approval never
bypasses a runtime policy denial.

When a second theme is supported, also switch away during active work, return,
and verify preserved conversations plus a traceable context handoff. This is a
later-theme acceptance check, not a claim of first-release functionality.
