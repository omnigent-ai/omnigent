---
service: omnigent
created: 2026-10-11
---

# End Forum discussion by unanimous agreement

## Intent

Allow agents to end a discussion before its maximum completed-response count
when all participating agents agree that no further work is needed. The human
still decides whether the result is satisfactory.

## Context

Approved for implementation on 2026-10-11. ForumState now also ends loops on unanimous fresh completion votes.
It also supports whole-forum suspension and individual participant quarantine.
The server coordinates sequential turns and fences stale completions.

## Requirements

- R1: Unanimous agreement can stop scheduling further agent turns.
- R2: Agreement must be explicit, attributed to the completing participant,
  and based on the current discussion rather than stale votes.
- R3: Human guidance and participant membership changes invalidate agreement.
- R4: The UI explains consensus progress and why the forum stopped.
- R5: Existing stop, pause, retry, response-limit, and restart semantics remain
  effective; agent agreement does not certify human acceptance.

## Clarifications

- Q1: Do quarantined agents count toward unanimity?
  A1: Approved: exclude manually quarantined participants, requiring
  at least two remaining participants. Temporarily unavailable agents remain
  required; an outage is never consent.
- Q2: What resets agreement?
  A2: Approved: new human guidance, any completed CONTINUE/missing/invalid vote,
  failed or interrupted turns, participant pause/resume, and whole-forum
  pause/stop/restart. A new agreement sequence must collect every required
  participant's vote after the reset.
- Q3: Can humans restart a consensus-stopped forum?
  A3: Approved: yes, using a new message, continuing at the next participant with
  a fresh response allowance and no previous votes.

## Acceptance Criteria

- AC1: GIVEN three required agents WHEN all explicitly vote DONE after the last
  reset THEN the last reply is saved and the loop stops without another turn.
- AC2: GIVEN existing DONE votes WHEN an agent votes CONTINUE or supplies no
  valid vote THEN all existing votes are cleared and discussion continues.
- AC3: GIVEN a running turn WHEN new human guidance arrives THEN its old-context
  vote cannot count toward the new agreement sequence.
- AC4: GIVEN an interrupted, failed, duplicate, or stale turn WHEN its result
  arrives THEN it cannot cast a valid vote or trigger consensus.
- AC5: GIVEN a quarantined participant WHEN the required membership changes
  THEN votes reset; temporarily unavailable required participants cannot be
  silently omitted from unanimity.
- AC6: GIVEN a consensus-stopped forum WHEN a human posts new guidance THEN
  discussion restarts at the next participant with fresh votes and allowance.
- AC7: GIVEN a saved agreement sequence WHEN the coordinator recovers THEN
  votes and their revision are restored without accepting stale completions.
- AC8: GIVEN a completed final reply WHEN consensus and the response limit are
  reached together THEN both constraints prevent another turn, with consensus
  reported as the stop reason.

## Constraints

Use the existing session and harness dispatch paths. Store optional fields with
backward-compatible defaults in Forum's persisted JSON state. Validate votes
only from the active turn's final output after authoritative completion.
Do not classify casual words such as “done” with another model. Agreement is
advisory about ending discussion, not approval for tools or changes.

## Out of Scope

Majority voting, a separate judge agent, automatic human acceptance, extra
summary turns after consensus, and changes to spending controls.

## Plan

1. Add canonical turn instructions in omnigent/runtime/prompt.py requesting a
   strict trailing vote record with DONE or CONTINUE and a server-issued turn
   identifier. DONE means endorsing the current result without introducing
   further work; changes, objections, or unresolved validation require CONTINUE.
2. Parse only the final trailing record for that exact turn. Missing, malformed,
   conflicting, quoted, or wrong-turn records cannot grant consent. Strip valid
   metadata from rendered prose and expose the decision as a message badge.
3. Extend ForumState with a consensus revision, per-participant votes, a
   turn-start revision, and a stop reason. Every reset advances the revision.
   Apply completion, vote updates, response counting, and stop decisions in the
   same existing store transaction. Preserve normal turn-token fencing.
4. Require one DONE vote from every required participant since the latest reset.
   Duplicate harness/model instances remain distinct voters by session ID.
   Votes do not expire simply with time; errors and changes reset them.
5. Return progress and stop reason through the existing Forum API. Show
   “2 of 3 agents ready to finish,” per-message decisions, and “Stopped by
   unanimous agreement.” Ordinary Stop remains available at all times.
6. Validate deterministic scheduling, API permissions, UI transitions, older
   saved forums, concurrent guidance, outages, duplicate events, and restart.
   Exercise desktop/mobile and two harnesses with controlled outputs.

## Tasks

- [x] T1: Resolve quorum membership and finalize the vote protocol.
- [x] T2: Implement prompt instructions and strict final-record parsing.
- [x] T3: Add durable vote revisions, resets, atomic consensus termination,
  and restart handling to the coordinator/state machine.
- [x] T4: Expose consensus progress and reason in API and Forum UI.
- [x] T5: Update Forum requirements and feature-map verification steps.
- [x] T-val: Verify acceptance criteria with state/API/component tests and
  desktop/mobile browser journeys; report mixed-harness coverage separately.

Implementation verification: 49 Forum backend tests and 17 Forum UI tests pass.
Coordinator tests cover standard and native completion events with controlled
responses. TypeScript, Pyrefly, Ruff, frontend lint, and production build pass.
Live vendor adherence to the vote format remains separate from these tests;
missing or malformed votes safely continue discussion.

An isolated browser fixture verified the saved consensus state, two DONE badges,
stripped vote metadata, stopping at 2/100 responses, desktop/mobile rendering,
mobile list/navigation, and direct links. Evidence: /tmp/forum-consensus-evidence.
This fixture does not exercise live provider generation or native mobile wrappers.
