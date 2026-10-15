---
service: omnigent
created: 2026-10-07
---

# Forum — Product Requirements

## Intent

Let a user post a topic or task to a Forum where multiple agents collaborate
and discuss it. The primary first-release use case is a difficult programming
task: agents review and refine proposed solutions, with the human user as the
final judge of the outcome's quality.

## Context

Status: implemented on `hossein-exp-1`; verification results and deployment
limitations are recorded below.

Forum uses ordinary Omnigent sessions for harness execution, tools, approvals,
and access control. A parent session owns the forum's permissions and title;
each numbered participant has a separate child session on the same host and
workspace. Server-owned state records the discussion and coordinates turns.

## Requirements

Confirmed from the user's request:

- R1: A user can post a topic or task to a Forum.
- R2: A Forum supports multiple agents collaborating and discussing the posted
  topic or task.
- R3: The web and mobile UI navigation includes a link labeled "Forum",
  positioned below New session, Inbox, and Canvas in the left navigation.
- R4: Selecting "Forum" opens a Forums page listing existing forums.
- R5: The Forums page provides an action to start a new forum.
- R6: The new-forum flow presents supported harnesses and the supported models
  for each harness, allowing users to select harness–model pairs using checkboxes.
- R7: The user must select at least two agent participants, each configured
  with a harness–model pair. The minimum counts participant instances, not
  distinct harnesses or distinct pairs.
- R8: A forum supports participants selected from different harnesses.
- R9: A newly created forum opens blank. The human user posts the first
  question or task inside the forum; agents begin responding after that post.
- R10: Each agent can see all prior responses from all other forum
  participants when contributing to the discussion, including the preceding
  agent's completed response. Ordinary human posts arriving during generation
  become context for the next agent's turn.
- R11: The human user can post in the ongoing discussion to guide it, and
  those posts form part of the shared discussion available to agents.
- R12: Every discussion post clearly identifies its author, distinguishing
  human posts and individual agent participants.
- R13: For difficult programming tasks, participating agents can review one
  another's responses and iteratively refine proposed solutions.
- R14: The human user is the final judge of the outcome's quality. Agent
  agreement alone does not establish that a solution meets the user's needs.
- R15: Each forum is dedicated to one specific task or question, with its
  discussion focused on that subject.
- R16: Forum agents retain the permissions and capabilities available to them
  in normal Omnigent sessions, including tool execution and file changes where
  their session configuration permits them. Existing approval and policy
  requirements continue to apply.
- R17: Multiple human users can participate in the same forum, posting
  contributions and guidance alongside the agents. Each human's posts are
  attributed to that individual and included in the shared discussion context.
- R18: Forum visibility, sharing, and human participation permissions match
  normal Omnigent sessions, using the same access rules and permission semantics.
- R19: Each forum receives an automatically generated name, following the
  naming behavior of normal Omnigent sessions. The Forums list identifies
  forums by these names.
- R20: Multiple models from the same harness can satisfy the participant
  minimum. An identical harness–model pair can also be added more than once,
  with each instance treated as a separate, distinguishable participant.
- R21: During participant selection, each agent instance is assigned a visible
  order number defining its position in the discussion sequence.
- R22: After the initial human post, agents respond one at a time in numbered
  order. The next agent begins after the preceding agent completes its turn.
- R23: After the last agent completes its turn, the sequence returns to the
  first agent and repeats automatically until the user stops the discussion
  or the configured maximum number of agent responses is reached.
- R24: The human user can stop the discussion by clicking a Stop button.
- R25: The human user can also stop the discussion by posting a message
  expressing that intent, such as "that is enough" or "thank you all".
  Recognition is based on the human's intent in context; the examples are
  not an exhaustive list of exact commands. Once a stop request is recognized,
  no further agent turns are started.
- R26: Both stop methods immediately interrupt the currently active agent
  rather than waiting for its turn to finish, and prevent subsequent turns.
- R27: A forum must be resilient to individual agent outages. One agent's
  outage must not cause loss of the shared discussion or require recreating
  the forum.
- R28: On an individual agent outage, use bounded retries, then skip the
  unavailable agent and continue with healthy participants in their relative
  numbered order. Display the outage status to users.
- R29: A recovered agent automatically rejoins at its normal position in the
  sequence with the current discussion history available as context.
- R30: Pause the discussion loop when fewer than two agents are available.
  Automatically resume once at least two agents are available again. Recovery
  must not override an explicit human stop or an exhausted response limit.
- R31: All agent participants in a forum use a single shared workspace so
  they can access one another's files and changes. Participants do not use
  separate per-agent worktrees; normal session permissions still apply.
- R32: An ordinary human message posted during an agent's turn does not
  interrupt or steer that active turn. It is included in the next agent's
  discussion context. The Stop button interrupts immediately; explicit
  stop-intent messages retain the stopping behavior defined in R25–R26.
- R33: Only configured, ready-to-run harness–model options are selectable
  when adding forum participants.
- R34: During forum setup, the user selects a maximum number of agent
  iterations, defaulting to 100. An iteration is one completed agent response,
  counted across all participants, not a full round or an internal tool call.
- R35: After the configured number of agent responses, the forum automatically
  stops the discussion loop without starting another agent turn. Human posts
  do not count toward this maximum.
- R36: A new human message can restart a stopped discussion, continuing with
  the next agent in numbered order rather than returning to agent 1. This
  resets the completed-response counter to zero, granting the full configured
  allowance again, including after a stop caused by reaching the limit.
  Automatic recovery from an outage pause preserves the existing count.
- R37: Only completed agent responses count toward the response limit.
  Failed or interrupted turns do not increment the counter, even if partial
  output was produced.

Additional requirements will be recorded as decisions are made. Questions
below are not implied requirements or agreed defaults.

Consensus completion is extended by [forum-consensus.md](forum-consensus.md):
fresh explicit DONE votes from every non-quarantined participant (at least two)
can stop discussion early. An outage does not remove an agent from the quorum.
The human still judges the result and can restart with new guidance.

## Implementation decisions

These are implementation defaults within the agreed requirements, rather than
additional requirements supplied by the user:

- Setup selects an online host, a shared workspace path, 2–32 participant
  instances, and a completed-response limit from 1–10,000 (default 100).
  The picker lists only configured harnesses and their reported model catalog.
- Selection order assigns turn numbers. “Add another” duplicates a pair;
  removing and re-adding an instance changes its order before creation.
  Participants stay fixed after creation; existing sessions are not imported.
- The list shows automatic title, participant count, discussion state, and
  completed-response count. Selecting an entry reopens its saved discussion.
- Posts display a human name or agent identity, an avatar, timestamp, and
  rendered Markdown. Humans choose a display name in the composer; messages
  persist it separately from their authenticated identity. A live indicator
  above the composer names the active agent, or explains preparation/recovery.
  Each participant links to its ordinary session for tool output, files, and
  approval controls. The existing Inbox remains available for approvals.
- Sharing uses the normal session permission dialog with a forum URL. Multiple
  humans with edit access can post and stop; readers can view. Agent messages
  cannot change human permissions or trigger the stop-intent detector.
- Failure retries wait 2 and 4 seconds; after another failure the participant
  is skipped for 30 seconds before it is eligible to retry at its normal place.
  Fewer than two eligible participants pauses the loop. Recovery probes use
  normal dispatch, so an expired cooldown does not itself prove provider health.
- A disconnected host is detected within a five-second active-turn check.
  A 30-minute watchdog interrupts a turn with no terminal result (including a
  turn left waiting for approval). Failed/interrupted turns consume no allowance.
- Common standalone stop phrases are recognized immediately. Other messages
  use the existing isolated inference service for intent classification. If
  inference is unavailable or ambiguous, preserve the message as guidance;
  the Stop button remains deterministic.
- Discussion and counters persist in the operational database. Leases serialize
  workers and turn tokens reject late output. An editor reopening an active
  forum recovers a worker after a server restart. Recovery preserves counts;
  a new human message after an explicit stop or limit resets the count and
  starts with the next agent.

## Acceptance Criteria

Acceptance criteria for the agreed feature:

- AC1: GIVEN a user starting a Forum WHEN they submit a topic or task THEN
  that input becomes the subject of the Forum's discussion. (R1)
- AC2: GIVEN a Forum with at least two participating agents WHEN they discuss
  its topic or task THEN multiple agents can contribute to the discussion. (R2)
- AC3: GIVEN the web or mobile UI navigation is open WHEN the user views its
  primary links THEN "Forum" appears below New session, Inbox, and Canvas. (R3)
- AC4: GIVEN the user can see the "Forum" navigation link WHEN they select it
  THEN the Forums page opens and lists existing forums. (R4)
- AC5: GIVEN the user is on the Forums page WHEN they choose to start a new
  forum THEN they can enter the forum creation flow. (R5)
- AC6: GIVEN the user is creating a forum WHEN they view participant selection
  THEN supported models are presented under their respective harnesses with
  checkboxes for selecting harness–model pairs. (R6)
- AC7: GIVEN fewer than two participant instances are selected WHEN the user
  attempts to create the forum THEN creation is prevented and the minimum of
  two participants is explained. (R7)
- AC8: GIVEN the user has selected at least two supported harness–model pairs
  across different harnesses and met other creation requirements WHEN they
  create the forum THEN each selected pair is a participant. (R7, R8)
- AC9: GIVEN a newly created forum WHEN it opens before the first human post
  THEN its discussion is blank and agents have not begun responding. (R9)
- AC10: GIVEN a blank forum with selected agents WHEN the human posts the
  first question or task THEN the agents begin responding to that input. (R9)
- AC11: GIVEN other participants have already posted responses WHEN an agent
  begins a contribution THEN all those prior responses are available to it
  as discussion context. (R10)
- AC12: GIVEN an ongoing discussion WHEN the human posts guidance THEN it
  appears in the discussion and is available to subsequent agent
  contributions. (R11)
- AC13: GIVEN a discussion containing human and agent posts WHEN the user
  reads a post THEN its author is clearly identifiable, including which
  individual agent authored an agent post. (R12)

- AC14: GIVEN a programming task with a proposed solution in the forum WHEN
  another participating agent contributes a review THEN it can critique that
  solution and propose improvements in the shared discussion. (R13)
- AC15: GIVEN participating agents agree on a proposed solution WHEN their
  agreement is presented THEN it is attributed to the agents and is not
  represented as the human user's acceptance. (R14)
- AC16: GIVEN a forum has been started for a specific task or question WHEN
  participants review and refine responses THEN their contributions belong
  to the discussion of that forum's task or question. (R15)
- AC17: GIVEN an agent's configuration permits an action in a normal Omnigent
  session WHEN it attempts that action as a forum participant THEN the action
  is available under the same permission, approval, and policy rules. (R16)
- AC18: GIVEN an action requires approval or is denied in an agent's normal
  session configuration WHEN that agent attempts it in a forum THEN the same
  approval requirement or denial applies. (R16)
- AC19: GIVEN two human users have access to participate in a forum WHEN
  each posts a contribution THEN both posts appear in the same discussion,
  identify their respective authors, and are available as context to subsequent
  agent contributions. (R10, R11, R12, R17)
- AC20: GIVEN a user has a particular access level WHEN they attempt to view,
  share, or post in a forum THEN the action is allowed or denied under the
  same rules as for a normal Omnigent session with that access level. (R18)
- AC21: GIVEN a forum reaches the point at which normal session naming
  generates a title WHEN automatic naming completes THEN the forum receives
  a generated name and that name appears in the Forums list. (R19)
- AC22: GIVEN two participants use different supported models on the same
  harness and other creation requirements are met WHEN the user creates the
  forum THEN the two participants satisfy the minimum and creation succeeds.
  (R7, R20)
- AC23: GIVEN two participants use an identical supported harness–model pair
  and other creation requirements are met WHEN the user creates the forum
  THEN both instances count toward the minimum and appear as separate
  participants with distinguishable post attribution. (R7, R12, R20)
- AC24: GIVEN the user selects agent participants WHEN the participant list
  is displayed THEN each agent instance has an order number showing its
  position in the response sequence. (R21)
- AC25: GIVEN a forum with numbered participants WHEN the human submits the
  first question or task THEN agent 1 responds first and each subsequent
  agent begins only after its predecessor completes, with prior responses
  available as context. (R10, R22)
- AC26: GIVEN the discussion loop is running, the user has not stopped it,
  and the response limit will not be reached by the current turn
  WHEN the final numbered agent completes its turn THEN agent 1 begins the
  next round without requiring another human post. (R23)
- AC27: GIVEN a discussion loop is running WHEN an authorized human user
  clicks Stop THEN the active agent is immediately interrupted and the loop
  stops scheduling further agent turns. (R24, R26)
- AC28: GIVEN a discussion loop is running WHEN an authorized human user
  posts "that is enough" or "thank you all" to indicate the discussion should
  end THEN that intent is recognized, the active agent is immediately
  interrupted, and no further agent turns are started. (R25, R26)
- AC29: GIVEN a forum has existing discussion history WHEN an individual
  agent becomes unavailable THEN the history remains accessible and the forum
  does not need to be recreated to recover from that outage. (R27)
- AC30: GIVEN an agent's bounded retries are exhausted and at least two other
  agents remain available WHEN its outage is handled THEN its unavailable
  status is displayed and the loop continues with healthy agents in their
  relative numbered order. (R28)
- AC31: GIVEN an unavailable agent recovers while the discussion loop is
  running WHEN its next normal position is reached THEN it can contribute
  again with the current discussion history available. (R29)
- AC32: GIVEN outages leave fewer than two agents available WHEN the scheduler
  would start another turn THEN it pauses the loop instead. (R30)
- AC33: GIVEN one agent writes a file in the forum's workspace WHEN another
  participant accesses that path on its turn THEN it sees the same file and
  the preceding agent's saved changes, subject to normal permissions, without
  a worktree merge or manual transfer. (R31)
- AC34: GIVEN an agent is responding WHEN a human posts ordinary guidance
  THEN the active turn continues uninterrupted and the next agent receives
  that guidance along with the shared discussion history. (R10, R11, R32)
- AC35: GIVEN the user is selecting forum participants WHEN a harness–model
  option requires installation, authentication, or other configuration before
  it can run THEN that option cannot be selected. (R33)
- AC36: GIVEN the user is setting up a new forum WHEN the iteration-limit
  field is displayed THEN its default is 100 agent responses and the user
  can configure a different maximum. (R34)
- AC37: GIVEN a running forum with a maximum of 100 responses WHEN the 100th
  agent response completes THEN the loop stops and no 101st agent response
  is started, regardless of how many full rounds have completed. (R34, R35)
- AC38: GIVEN a forum has a configured response maximum WHEN a human posts
  guidance THEN that post does not increment the agent-response count. (R35)
- AC39: GIVEN a discussion stopped by the user or by reaching its limit WHEN
  a human posts a new message to continue the discussion THEN the loop
  restarts with the next agent in numbered order, resets the completed-response
  counter to zero, grants the full configured allowance, and includes the new
  message in its discussion context. (R36)
- AC40: GIVEN a forum is paused solely because fewer than two agents are
  available and response allowance remains WHEN at least two agents become
  available THEN the discussion resumes automatically without a human post.
  (R30)
- AC41: GIVEN a forum has been explicitly stopped or has reached its response
  limit WHEN an unavailable agent recovers THEN recovery alone does not
  restart the discussion. (R30)
- AC42: GIVEN a forum has recorded N completed responses WHEN an agent turn
  fails or is interrupted, including after partial output THEN the response
  counter remains N. (R37)
- AC43: GIVEN a forum has recorded N completed responses WHEN the next agent
  response completes successfully THEN the counter becomes N + 1 and the
  configured limit is evaluated. (R34, R35, R37)
- AC44: GIVEN a forum paused by an outage has N completed responses below its
  limit WHEN it automatically resumes after recovery THEN the counter remains
  N and only the remaining allowance is available. (R36)


## Constraints

- Use normal Omnigent session access, tools, approvals, and sharing policy.
- All participants use one workspace on the selected host. There are no forum
  worktrees, automatic merges, or additional tool permissions.
- Discussion history is supplied to every turn; provider context limits still
  apply. The response cap bounds the loop, not tokens or spend.

## Out of Scope

Multiple topics per forum, invitations to existing agent sessions, changing
participants mid-discussion, special mention routing, automatic acceptance of
an outcome, and separate spending/time-budget controls.

## Technical plan

- Persist tenant-scoped forum state and worker leases in a migrated table.
- Reuse session creation and event dispatch for participant harnesses.
- Compose forum instructions at the runtime prompt boundary.
- Schedule ordered turns, fence interruption, retry outages, and enforce the
  completed-response limit on the server.
- Expose authenticated forum APIs and responsive list/setup/discussion pages.
- Cover lifecycle, creation cleanup, permissions, UI controls, and native
  completion separately, then exercise the isolated application.

## Verification

Automated coverage lives in `tests/server/test_forum.py` and
`web/src/pages/ForumsPage.test.tsx`. Session status and sharing tests provide
regression coverage. Manual entry-point recipes are in `feature-map/forums.md`.

Verified on 2026-10-07:

- 392 focused backend, runner, migration, sharing, and feature-map checks passed.
  Three additional guidance-timing cases passed, covering immediate visibility
  during classification and fencing a delayed stop after restart.
- 182 frontend Forum, sidebar, and permissions tests passed. The later UX
  refinement passed 12 Forum component tests and 30 backend Forum tests, with
  browser checks for saved human names and thinking/stop indicators. TypeScript, Python type
  checks, lint, formatting, and the production web build passed.
- An isolated browser with mock inference and real Codex processes completed
  turns 1 → 2 → 1, stopped at a limit of three, restarted with a fresh allowance,
  and stopped via both the button and “thank you all.”
- Desktop navigation, mobile drawer navigation, setup, discussion layout, and
  reopening the forum route passed browser checks.
- The mixed Codex/Claude browser journey reached Claude with Codex's response
  in its prompt, but the local Claude CLI returned a credential error before
  producing output. Full mixed-harness browser completion remains unverified;
  native completion/failed/cancelled contracts pass automated tests.

Process restart recovery currently requires an editor to reopen the forum;
automatic recovery across a full server restart is not a startup job. Cross-
replica event delivery and live vendor outages require deployment verification.
Native mobile application wrappers require device verification separately from
the responsive browser layout.
