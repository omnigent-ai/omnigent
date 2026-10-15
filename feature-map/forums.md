# Forums

A Forum lets humans and multiple agent participants work through one task in
an ordered discussion. Each participant uses its normal harness, model, tools,
and approvals in a shared workspace. Humans guide the next turn or stop the
active turn; only completed responses consume the configured allowance. Fresh
unanimous agent agreement can end discussion early; the human still judges
the outcome. Forums is experimental and disabled by default.

## Sub-features

- `feature-flag`: deployment-wide opt-in, experimental navigation label, and
  hidden navigation/unavailable pages and APIs when disabled; saved data remains.
- `list`: empty/loading/error states and reopening named forums; Forum parent
  and participant sessions are hidden from the sidebar session lists.
- `rename`: automatic titles and editor-controlled title changes inside a Forum;
  clicking its title in the list still opens the discussion.
- `workspace`: the standard session info popover and desktop/mobile workspace
  controls, including Agents, Files, Changes, and supported shell/browser views.
  Forum cost and per-model token usage include its participant session tree.
- `setup`: ready harnesses/models, duplicate instances, order, shared workspace,
  participant display names, maximum completed responses (default 100), and
  explanations for disabled creation below the two-participant minimum.
- `discussion`: blank task prompt, attributed Markdown posts, active participant,
  human display names, a live thinking/recovery indicator above the composer,
  human guidance for the next turn, and participant session links.
- `stop-restart`: button or stop-intent message; new guidance restarts at the next
  participant with a fresh allowance.
- `consensus`: explicit fresh agreement from every non-quarantined participant
  (at least two), progress and reply badges, and a distinct unanimous stop reason.
- `outages`: bounded retries, visible unavailable state, skip/rejoin, and automatic
  pause/recovery when fewer than two participants are eligible.
- `pause`: Pause beside Stop holds the whole forum until Resume, preserving the
  response count and retrying the interrupted participant. Guidance stays queued.
- `quarantine`: editors pause/resume individual participants; inactive agents are
  skipped until resumed, and fewer than two available agents pauses the loop.
- `delete`: owner-only confirmed deletion of discussion and participant sessions;
  shared workspace files remain.
- `sharing`: session-equivalent read/edit/manage access and forum share links.

## How to get to it (user POV)

**Desktop web:** left navigation → Forums (above Inbox, with an experimental
label) → New forum or an existing forum.

**Mobile web:** open the navigation drawer → Forums (above Inbox) → New forum or an
existing forum. Native app wrappers use the same web routes but need device checks.

**Shared link:** open a copied `/forums/<id>` link; access follows the parent
session's permissions. Participant links open ordinary session tool/approval views.

**Direct pages:** `/forums` opens the list and `/forums/new` opens creation.
All Forum routes require the feature flag, including direct and shared links.

## Driving it with the repro environment

Preconditions: enable the deployment flag with `OMNIGENT_FEATURES=forums`, then
start the isolated environment using the [verification skill](skills/verify-omnigent/SKILL.md);
build the current web UI. Configure at least one ready harness and two model
instances on the isolated host. Claude/Codex mock journeys also require their CLIs.

For both desktop navigation and the mobile drawer, select Forums, check the empty
list, then create a forum. Select a model and use Add another so the same pair
appears twice. Enter a display name for each instance in Response order; leave
one blank to keep its harness/model label. Verify the names appear in the
discussion and thinking indicator after creation. Verify numbered order and a
disabled Create button below two participants. Also select different models
from one harness and, when configured, participants from two different harnesses.
Enter an isolated workspace and set the limit to 3. The new forum
must open blank. Enter Your name and post a small programming task. Verify the
post uses that name without a Human subtitle, and reloading restores the name.
While a turn is active, verify the thinking indicator above the composer names
the responding agent. Use CONTINUE votes for this limit check so consensus does
not stop it early. Observe participants 1, 2, 1 respond, the counter stop at 3,
and the thinking indicator disappear. Read-only cross-check: GET `/v1/forums/<id>`.

Post new guidance and verify the count resets and participant 2 goes next. Stop
while an agent is responding; no interrupted response should increment the
counter, and the thinking indicator should disappear. Restart, then post
“thank you all” and check the stopped state. Open a
participant session to inspect its normal tools and approvals. Reopen the forum
from the list and check saved posts.

For shared-link entry, give another test user read access and verify they can
read but cannot send or stop. Grant edit and verify separately attributed human
posts. Copy the forum link and reopen it at desktop and mobile widths.

Automated contracts: `tests/server/test_forum.py` covers state, coordinator,
permissions, API behavior, renaming, and consensus; `tests/db/test_migration_forums.py`
covers the database migration. `web/src/pages/ForumsPage.test.tsx` covers Forum
components. These tests do not require a running repro instance. The desktop,
mobile drawer, and direct/shared-link journeys above and below require browser
verification; component tests alone do not prove those entry points.

Open an existing forum, choose Delete forum, and cancel to verify no deletion.
Confirm Delete permanently and verify return to the list with the forum gone.
Readers and editors must not see the delete action. Repeat on mobile web.

Pause the responding participant using its header pause icon. Verify it becomes
Inactive, its interrupted response is not counted, and the other agents continue
in order. Resume it and verify it rejoins. With two participants, pausing one
must hold the loop until resumed. Readers cannot operate these controls.

With an agent responding, click the whole-forum Pause button beside Stop.
Verify the turn is interrupted without incrementing the counter. Post guidance
and check that it is saved while the discussion remains paused. Click Resume:
the interrupted participant should retry with that guidance and the same
response allowance. Recovery of an unavailable agent must not override an
explicit whole-forum pause or stop. Repeat on mobile web.

Using controlled failures, verify bounded retries, then skipping an unavailable
participant while at least two others can respond. Its error/recovery state
must be visible. With fewer than two available participants, verify automatic
pause and automatic recovery once two are available. Failed turns must not
increment the counter; a recovered participant rejoins in the ordered loop.

Open a forum through desktop navigation, the mobile drawer, and a direct link.
Verify its backing session is absent from Sessions (including pinned/project
groups). Open the top-right info surface to inspect cost and per-model usage;
open Agents in the workspace sidebar to inspect the participant tree. Verify
that an ordinary session remains listed and that switching from it to a Forum
does not display the ordinary session's usage. Workspace tools retain normal
session availability and access checks.

For consensus completion, follow the behavior specified in
[the consensus spec](../specs/features/forum-consensus.md). Use controlled final
responses that follow the current turn's supplied vote instructions. Exercise
these scenarios on desktop and mobile, including opening saved Forums by direct
link:

- Have three required participants explicitly vote DONE after the last reset.
  Verify “1 of 3 agents ready to finish” progresses to unanimity, reply badges
  say “Ready to finish,” and valid vote metadata is absent from rendered prose.
  Save the last reply and stop with “Stopped by unanimous agreement,” without
  scheduling an extra turn or summary. Repeat with a limit of 3: consensus must
  be the displayed reason when agreement and the response limit coincide.
- After one DONE, supply CONTINUE, a missing vote, or an invalid vote. Verify
  progress resets and discussion continues with a “Continue discussion” badge.
  Casual prose saying “done,” quoted/fenced records, conflicting records, and
  records for another turn must never grant consent. DONE endorses the current
  outcome; proposed changes, objections, or unresolved checks require CONTINUE.
- Post human guidance during a turn after a prior DONE. Verify progress clears;
  the in-flight reply can finish, but its old-context DONE cannot count toward
  the new agreement. Every required participant must agree again.
- Verify failed or interrupted turns, participant pause/resume, and whole-forum
  pause/stop/restart clear agreement. Delayed or duplicate completion events
  must not add a vote, double-count a response, or stop the discussion.
- Quarantine one of three participants: votes reset and the remaining two must
  agree afresh. With only one non-quarantined participant, consensus cannot
  finish the Forum. Restore the third and verify another reset. A temporarily
  unavailable participant remains required; an outage never counts as consent.
  Duplicate harness/model instances each need their own vote.
- Reload a partially agreed Forum and recover its coordinator in the isolated
  environment. Verify saved progress survives, old turn results cannot count,
  and a saved Forum predating consensus fields loads with no votes. Votes do
  not expire merely because time passes.
- Post new human guidance after a consensus stop. Verify the next participant
  resumes with zero completed responses, a full allowance, and no prior votes.
  The human's Stop control remains available during active discussion.

State and coordinator tests cover the completion-event and recovery cases with
controlled standard/native responses. Browser evidence must separately show
progress, badges, stop reason, restart, and desktop/mobile/direct-link entry
points. Live adherence across two harnesses and native mobile wrappers needs
separate verification; a saved-state browser fixture does not establish it.

Click the title inside a forum to rename it. Save with Enter or Save; cancel
with Escape or Cancel. Blank names are rejected and readers cannot rename.
Reload to verify persistence, then select the renamed entry in Forums to verify
it still opens the discussion. Repeat at mobile width.

For the feature flag, first start an isolated server without `forums` enabled.
Verify the desktop link and mobile drawer entry are absent, all three direct
page routes show Not Found, and Forum API requests return 404. Restart with
`forums` enabled, refresh, and verify navigation (including the experimental
label), creation, and saved Forums are available again. Preserve any other
enabled flags in the comma-separated value. Automated flag contracts live in
`tests/server/test_feature_flags.py`, `tests/server/integration/test_utility_endpoints.py`,
`tests/server/test_forum.py`, `web/src/App.test.tsx`, and
`web/src/shell/Sidebar.test.tsx`. Session-list and workspace integration are
covered by `web/src/hooks/useForumSessionIds.test.ts` and
`web/src/shell/AppShell.test.tsx`.

## Gotchas

- Forums is disabled by default. Without the forums release flag the navigation
  is hidden, direct Forum pages show Not Found, and all Forum APIs return 404.
  Restart the server after changing `OMNIGENT_FEATURES`. Existing data is retained.

- A response allowance counts completed agent responses, not whole rounds.
- Guidance does not interrupt; Stop does. Explicit stops override recovery.
- Whole-forum Pause preserves the counter and retries the interrupted agent;
  restarting a stopped Forum resets the counter and advances to the next agent.
- A reply's DONE badge records that reply's decision, not necessarily a current
  vote: later guidance or another reset can invalidate it. Read the current
  agreement progress separately.
- Consensus ends scheduling only. It does not approve tools or changes, certify
  human acceptance, use majority voting or a judge agent, or alter spending controls.
- Duplicate pairs are independent sessions and retain distinct order numbers.
- Failed harness authentication is not fixed by waiting for a retry timer.
- A browser refresh must not reset the counter. After a server restart, reopening
  an active forum as an editor restarts its coordinator with persisted state.
- Responsive-browser proof does not verify native iOS/Android app wrappers or
  live-provider recovery. Keep these verification claims separate.
