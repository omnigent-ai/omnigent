---
service: omnigent
created: 2026-10-28
---

# Add inline GitHub PR conversations

## Intent

Read and reply to GitHub review conversations beside the code in the PR tab.
Build toward the familiar GitHub interaction: open a thread below a line, reply
in place, start a new conversation from the gutter, and resolve completed work.

## Context

[PR #11546](https://github.com/omnigent-ai/omnigent/pull/11546) added top-level
conversation comments, Markdown preview, account attribution, in-memory drafts,
and posting through the existing server → runner/host → GitHub path. It explicitly
excluded review-thread replies.

At the inspected checkout, the user's “Diff” subtab is labeled **Changes**.
`PullRequestPanel.tsx` renders a stacked `@pierre/diffs` `FileDiff` per file,
with unified/split layouts, lazy mounting, file collapse, and context expansion.
The pinned version, 1.3.6, exposes `lineAnnotations`, `renderAnnotation`,
`renderGutterUtility`, and line-selection callbacks. No renderer replacement or
dependency upgrade is planned. The hooks were verified against installed types;
their layout and focus behavior still need a browser prototype.

The provider abstraction serves GitHub, GitLab, Azure DevOps, and extensions.
Top-level comments are part of PR info; there is currently no review-thread
resource. The existing per-session file comments target the agent and have a
different lifecycle from GitHub conversations.

The diff query is keyed by base/head revision, but its patch request currently
fetches the latest PR without passing or returning those revisions. Expanded
file contents already validate requested revisions. Inline anchors make closing
the patch snapshot gap a prerequisite.

## Requirements

- R1: Show published GitHub review threads below their referenced lines in the
  PR's Changes subtab, in both unified and split layouts.
- R2: Show the author, Markdown body, timestamp, GitHub permalink, replies, and
  resolved/outdated state. Preserve independent threads at the same location.
- R3: Show a compact reply input below an existing thread. Focus expands the
  Markdown composer with the posting account visible; Cancel collapses it while
  retaining the draft. A reply publishes immediately to that GitHub thread.
- R4: Preserve drafts and pending/error state across tab, file-collapse, PR,
  layout, and session switches. Deduplicate submissions and retain text after
  failure. Page-reload persistence is not required.
- R5: Keep outdated, file-level, and otherwise unplaceable conversations
  discoverable without inventing an anchor in the current code.
- R6: Include the first thread page in the existing PR-info request while Changes
  is visible, using one coordinated poll and one combined GitHub PR-data query.
  Support on-demand pagination, refresh, external updates, and explicit
  partial/error states without a second periodic thread request.
- R7: Use the selected PR, host, and GitHub account. Enforce session permissions,
  provider capabilities, and thread ownership of the selected PR at write time.
- R8: Add a gutter “+” to create an immediately published review comment on an
  eligible line or same-side range of the displayed PR revision. Keep new
  conversations separate from existing threads at the same location.
- R9: Allow resolve/reopen where GitHub reports that the selected account can do so.
- R10: Keep controls usable by keyboard and touch and in the narrow mobile drawer.
  Retain the diff's lazy rendering and large-file protections.

## Clarifications

- Q1: What is the recommended delivery boundary?
  A1: Deliver R1–R8 and R10: existing threads with compact reply inputs and
  new line/range conversations. Resolve/reopen (R9) remains follow-up work.
- Q2: Should “Changes” be renamed to “Diff”?
  A2: Retain the current label; this plan uses Changes to identify the existing
  subtab. A rename is not needed for inline conversations.
- Q3: Which providers are included?
  A3: Implement GitHub first, including configured GitHub Enterprise hosts, with
  additive capability flags and unsupported behavior for other providers.
- Q4: Do replies become a pending GitHub review?
  A4: No. Replies and new comments publish individually, matching the immediate
  posting model introduced in #11546. Draft review submission is separate work.
- Q5: Can threads share the current request to avoid a second polling load?
  A5: Yes. Add an optional thread inclusion flag to the existing info request and
  combine metadata and threads in its GitHub GraphQL query. Lightweight consumers
  keep the existing payload when Changes is not visible. Additional pages are
  requested on demand. Larger responses and GraphQL work still have a cost;
  combining requests removes the extra periodic transport round trip.

## Acceptance Criteria

- AC1: GIVEN published threads on old and new lines WHEN Changes opens THEN each
  thread appears on the correct side and line in unified and split views.
- AC2: GIVEN several threads on one line or an existing multiline thread WHEN
  rendered THEN conversations remain distinct and a multiline thread identifies
  its range and appears after its ending line.
- AC3: GIVEN permission to reply WHEN a reply succeeds THEN it appears once in
  the original GitHub thread with the correct author and survives refresh.
- AC4: GIVEN a reply draft or in-flight request WHEN the user changes tabs, PRs,
  sessions, file collapse, or layout THEN its state stays with the original PR
  and thread and a late response cannot modify the newly selected conversation.
- AC5: GIVEN a rejected or uncertain write WHEN the result is handled THEN the
  draft remains, the UI explains the failure, and no automatic retry or second
  transport dispatch occurs after a possibly successful submission.
- AC6: GIVEN an outdated, file-level, deleted-file, renamed-file, hidden-context,
  or unavailable-diff thread WHEN Changes opens THEN it remains reachable with
  accurate context or an explicit fallback, never attached to unrelated code.
- AC7: GIVEN additional pages or a failed fetch WHEN threads are displayed THEN
  available conversations remain visible, remaining pages can be loaded, and
  incomplete counts or failures are distinguishable from no conversations.
- AC8: GIVEN a read-only session, an unsupported provider/host, or insufficient
  GitHub permissions WHEN the user views or attempts to mutate a thread THEN
  reads use session read access and writes are unavailable or rejected correctly.
- AC9: GIVEN a PR push during loading or composing WHEN revisions disagree THEN
  stale inline placement is withheld and new-comment submission requires refresh
  and reselection while preserving the text. Replies still target their thread ID.
- AC10: GIVEN an eligible line or same-side range and edit access WHEN the gutter
  “+” or selected-lines action posts THEN GitHub receives the displayed commit,
  path, side and range, and a separate conversation becomes visible without
  changing an existing thread or the top-level Comments feed.
- AC11: GIVEN GitHub permits resolving or reopening WHEN the action succeeds THEN
  the thread state and relevant counts update without losing an unsent reply.
- AC12: GIVEN any supported PR-panel entry point WHEN using keyboard or touch
  in a narrow viewport THEN the same thread/reply workflow remains reachable
  and the submit button and focus remain usable.
- AC13: GIVEN Changes and the session composer are mounted WHEN the regular
  PR-info poll runs THEN its single coordinated request includes a bounded thread
  page, its selected-PR data comes from one combined GitHub query, and no separate
  thread poll runs. WHEN Changes closes THEN periodic thread reads stop.

## Constraints

- Reuse existing account selection, provider dispatch, JSON Markdown transport,
  body validation, Markdown rendering, and non-idempotent posting safeguards.
- Keep GitHub as the source of truth. No database migration, duplicate persistent
  comment store, or coupling to agent-directed session comments is needed.
- Absent capabilities default to false, including on older hosts. Handle a newer
  host/client paired with an older server gracefully, retaining drafts on 404.
- Keep credentials on the execution host. Validate thread membership before
  accepting a GitHub node ID for a mutation.
- Keep network work bounded. Enrich the existing PR-info poll only when Changes
  is visible; composer metadata and Canvas must not independently request thread
  data or duplicate an in-flight enriched request for the same session/PR.
- Do not add a separate thread polling timer or one initial request per changed
  file. Bound the first page and load additional pages on demand.
- Do not silently omit unresolved discussions because their file or line is
  absent from the current patch.

## Out of Scope

- Pending review drafts, batch review submission, approve/request-changes flows.
- Creating multiline or file-level comments in the initial implementation;
  existing multiline and file-level threads remain readable and replyable.
- Editing/deleting comments, reactions, applying suggested changes, and uploads.
- GitLab/Azure DevOps discussion implementations and agent automation of replies.
- Draft synchronization across devices or survival across a browser reload.

## Plan

### Approach

**Interaction.** Expand unresolved, current threads beneath the relevant line.
Each card shows the original comment and replies, a GitHub link, and a compact
“Write a reply…” input that expands on focus. Cancel collapses the Write/Preview
composer while retaining its text; a successful post also collapses it. Resolved threads
start collapsed and can be expanded. Add a conversation count to each file header
and a compact next/previous unresolved-thread navigator in the diff toolbar.
Qualify counts while pages remain unloaded. On mobile, thread cards use the
available width and adding a comment must have a touch/focus action rather than
depend on hover. Keep thread controls outside the file-header collapse button.

**Read path.** Extend the existing PR-info resource with an optional
`include_review_threads=true` parameter, forwarded through `pr_resource`, the
provider facet, runner, and host fallback. Add a bounded `pr.review_threads`
envelope to that response when requested. Other info consumers retain the light
response; the new flag defaults to false.

The selected-PR reader currently uses `gh pr view --json`, which does not expose
`reviewThreads`. Replace the selected-PR data read with an explicit
`gh api graphql` query that requests existing metadata, checks, comments, and
optional `reviewThreads` together, using `@include(if: ...)` for thread data.
Reuse hostname/account helpers and normalize the result through existing shapers.
Preserve field-specific permission fallbacks, incomplete-data markers, and latest
top-level comments; do not keep the existing metadata request and append a
second GitHub thread request on every poll. Initial PR discovery, authentication,
permission fallbacks, and pagination may still require other calls.

GitHub's schema provides thread IDs, `path`, `diffSide`, `line`, `startLine`,
`startDiffSide`, original lines, `subjectType`, `isOutdated`, `isResolved`, and
viewer permissions. Fetch published comments with stable IDs, `fullDatabaseId`,
author, body, timestamps, URL, commit information, and `diffHunk`. Keep pending
review comments out of the published conversation view; respect minimized content.

Use bounded pages for both the thread connection and each thread's comments.
Load the first page with the enriched info request when Changes opens, and expose
“Load more conversations/replies” for additional pages. Do not automatically walk
all thread or comment pages on each poll.
Never treat the first 100 threads or replies as the complete collection. Preserve
the root comment's identifier independently of which reply page is visible.
Partial GraphQL data should preserve usable results and expose warnings.

**Revision consistency and placement.** Return base/head SHAs with threads in the
combined info response. Pass expected SHAs to patch and additional thread-page
reads. For the existing latest-PR patch operation, check PR revisions before and
after fetching and reject a moving snapshot. Compare response revisions before
combining a patch and its annotations. Restart a paginated read if the revision
changes between pages.
Carry the same revision through context expansion. This guards the newly visible
relationship between discussion and code, even before new comments are enabled.

Build a small pure mapping helper from GitHub coordinates to `DiffLineAnnotation`:
`LEFT` maps to `deletions`, `RIGHT` to `additions`, using actual file line numbers
rather than patch offsets. Use current/previous paths for renames, verify that
the requested side and line exist, and group multiple threads at a coordinate
without merging their identities. Render an existing multiline thread after its
end line and label the range.

Threads in hidden unchanged context use a visible file-level card with the original
snippet in this release. Expanding and anchoring arbitrary hidden lines is follow-up
work: it needs a bounded context-loading interaction to retain large-file protection.
Outdated, file-level, and unavailable-diff conversations use the same explicit fallback.

**Replies.** Add a resource taking `pr_url`, `thread_id`, and `body`. Resolve and
validate that thread against the selected PR, then use GitHub REST
`POST /repos/{owner}/{repo}/pulls/{number}/comments/{root_comment_id}/replies`.
The root ID comes from GitHub, including when replying below a later reply.
Use the REST write response to insert the canonical comment immediately;
a failed follow-up refresh must not turn a successful post into a failed post.
This also avoids accidentally starting a pending review through a GraphQL
review-comment mutation.

Extract shared presentation from `PullRequestCommentComposer` and shared draft/
posting mechanics only where both top-level and inline composers use them.
Reply drafts are keyed by session, canonical PR URL, and stable thread ID.
Keep drafts across revision changes; they belong to a conversation. Update only
the matching PR/thread cache and deduplicate by stable comment ID. Cancel stale
reads before inserting a successful reply so they cannot erase it.

Use `retry: false` and the existing `_post_pr_edit(non_idempotent=True)` behavior
for reply/create. An uncertain timeout preserves the draft and says to check
GitHub before retrying. Reuse permission checks, account attribution, and literal
JSON-body handling from #11546. An account change refreshes viewer permissions
and attribution before posting; invalidate or partition account-sensitive reads.

**Refresh and navigation.** Keep the existing PR-info poll as the single periodic
fetch owner. Lift the active subtab to that query's owner so it can request thread
inclusion while Changes is visible. Resolve inclusion centrally for the shared
session/PR query, rather than giving observers of the same query key conflicting
fetch functions or adding an independently polling enriched-query variant.
Lightweight consumers share the result and in-flight request. Opening Changes
requests an enriched refresh; closing it removes thread inclusion from later
polls. The ordinary metadata poll continues on other PR subtabs as it does today.

Derive the first thread page from info and cache additional pages by session, PR,
account, and revision. Distinguish a response that omitted thread data from an
empty or failed thread page. Preserve cached pages and drafts when a lightweight
response arrives, but mark pages stale on revision/account changes and explicit
refresh. Additional pages refresh on demand, with stale status visible; the
bounded first-page poll does not promise automatic freshness for all history.
Normal-sized discussions fit within the page and update on the existing cadence.

Panel Refresh, return to Changes, existing invalidation events, and successful
mutations use the same info request. Update successful writes from their response
before reconciliation. Keep the existing 10-second cadence and background/offline
gates; do not add a thread timer. Thread navigation expands the relevant file before scrolling/focusing; it also
reaches fallback cards.

**New conversations and resolution.** Use the gutter utility or selected-lines
action to open the composer beneath an eligible line or same-side range within
one diff hunk. Center the gutter plus over the divider between line numbers and
code as an absolute overlay, without adding gutter space.
Publish via GraphQL `addPullRequestReview` with `event: COMMENT`,
the displayed `commitOID`, and one thread carrying `body`, `path`, `side`, `line`
and optional `startLine`/`startSide`. Recheck both expected revisions before
dispatch. Preserve text and request reselection if the revision is stale.
An accepted comment may become outdated immediately after a concurrent push;
show it as such on refresh. Match the submitted review's root comment ID to its
canonical thread within the mutation response's last 25 threads and insert that
thread into the revision-matching cache. A missing match or unconfirmed submission
is an uncertain write, never retried automatically. Existing threads at the
coordinate remain independent. New drafts are keyed by session, PR and file.
Resolve/reopen remains deferred; it will use GitHub GraphQL
`resolveReviewThread`/`unresolveReviewThread`, gated by viewer permissions.

### Affected Components

| Component | Change |
| --- | --- |
| `omnigent/runner/git_providers/__init__.py` and provider facets | Add review-thread contracts and capabilities; keep unsupported providers compatible. |
| `omnigent/runner/github_resource.py` | Combined selected-PR GraphQL read with optional threads, pagination, reply/create/resolve calls, revision checks. |
| `omnigent/runner/pr_resource.py` | Selected-PR dispatch, shared body validation, coordinate validation. |
| `omnigent/runner/resource_routes.py` | Runner resources for reads and writes; revision parameters on patch reads. |
| `omnigent/server/routes/sessions/routes_resources.py` | Authorized proxies and existing non-idempotent write handling. |
| `omnigent/host/frames.py`, `omnigent/host/connect.py`, `omnigent/server/routes/_host_filesystem.py` | Host read/write operations so native and sleeping-runner sessions have parity. |
| `web/src/hooks/usePullRequests.ts` and focused review-thread hooks | Shared info-fetch inclusion, first-page projection, on-demand pagination, mutations, drafts, and invalidation. |
| `web/src/shell/PullRequestPanel.tsx` | Thread annotations, fallback groups, file counts, navigation, gutter integration. |
| `web/src/shell/PullRequestCommentComposer.tsx` and extracted shared comment UI | Reuse Markdown preview, authorship, validation, draft/error presentation. |
| `openapi.json`, `docs/git-pull-requests.md`, `feature-map/` | Generated API contract, provider capability documentation, GitHub verification recipe. |

### Data Model Changes

No persisted database changes. Introduce a separate review-thread transport type:

- Thread: stable opaque ID, path, subject type, side/range, original range,
  resolved/outdated state, viewer reply/resolve/reopen permissions, root comment
  ID, comments, comment cursor/completeness, and original context when available.
- Comment: stable opaque node ID and REST ID represented without numeric
  precision loss, author, body, timestamps, permalink, and publication state.
- Read envelope: canonical PR identity, observed base/head revision, thread page,
  next cursor, completeness, and warnings.

Keep top-level conversation comments in the existing `pr.comments` payload and
put the first thread-page envelope in optional `pr.review_threads`. Omission
means not requested or unsupported, not an empty conversation list; return an
explicit warning/partial state when a requested supported read fails.
New provider capability names: `review_threads`, `review_thread_replies`,
`review_thread_creation`, and `review_thread_resolution`, all false by default.
Use optional capability-specific facet contracts if adding required protocol
methods would invalidate installed third-party facets.

### API Changes

Proposed session resources under `/v1/sessions/{id}/resources/github`:

| Method and suffix | Purpose |
| --- | --- |
| `GET` at the existing info route | Accept optional `include_review_threads`; return metadata and a bounded thread page together. |
| `GET /review-threads` | Load additional thread pages on demand for `pr_url`, cursor, and expected revisions; no interval poll. |
| `GET /review-threads/{thread_id}/comments` | Page replies after verifying thread membership. |
| `POST /review-threads/{thread_id}/replies` | Immediately publish a reply with `pr_url` and `body`. |
| `POST /review-threads` | Create a line/range conversation with expected revisions and coordinates. |
| `POST /review-threads/{thread_id}/resolution` | Set resolved state with `pr_url` and `resolved`. |
| `GET /diff` (additive parameters/fields) | Accept expected base/head SHAs and return verified snapshot metadata. |

Mirror the resources through runner routes and host operation dispatch. Thread
reads require session read permission; mutations require edit permission and the
provider capability. The GitHub resource prefix remains the existing stable wire
convention. Do not repurpose the top-level `/comments` endpoint or session comment
store. Render a targeted unavailable state if an older server lacks the routes.

### Risks

- Snapshot validation adds GitHub calls. Share deadline budgets, fetch per PR
  rather than per file, and test push-during-fetch behavior explicitly.
- Replacing `gh pr view` for selected-PR info requires preserving check/reviewer
  permission fallbacks and adapting GraphQL connections to current shapers.
  Regression-test existing metadata and top-level comment behavior.
- One larger request does not make thread data free. Bound GraphQL connections,
  response bytes, and total query work; compare steady-state request counts and
  latency against today's panel with a representative busy PR.
- Shared cache observers must agree on inclusion and deduplicate reads. Verify
  the panel plus composer, mount/unmount, PR changes, and existing invalidations
  rather than assuming a boolean option alone prevents duplicate requests.
- Nested pagination can be expensive. Bound page sizes and response size, expose
  remaining pages, and preserve newly posted comments during refresh.
- Annotation sizing, split-pane alignment, shadow-DOM styling, lazy mounting,
  and focus need real-browser validation; component mocks alone cannot prove them.
- GitHub Enterprise schema versions and token scopes can differ. Treat unsupported
  fields and permission failures as a review-feature limitation without breaking
  the existing diff or top-level commenting. Confirm the supported version matrix
  during implementation.
- Never infer success or failure from a follow-up read after a successful write,
  and never retry an uncertain non-idempotent submission automatically.

## Tasks

### Phase 1 — Read existing threads

- [x] T1: Prototype annotations in the existing renderer with old/new-side threads,
  two threads on one line, a growing reply box, and both layouts. Verify mobile,
  keyboard focus, and expand-context behavior. Covers AC1, AC2, AC12.
- [x] T2: Add capability contracts, combined GitHub info/thread reads, optional
  inclusion through dispatcher/runner/server/host, and on-demand page resources.
  Covers AC7, AC8, AC13.
- [x] T3: Add patch/thread revision validation and response metadata, with a
  pure, tested line/path mapping helper. Covers AC1, AC2, AC6, AC9.
- [x] T4: Render thread cards, fallback groups, counts, and navigation; coordinate
  inclusion through the existing info poll and shared cache. Covers AC1, AC2,
  AC6, AC7, AC12, AC13.

### Phase 2 — Reply inline; first release

- [x] T5: Add reply validation and REST posting through both transport paths;
  preserve non-idempotent timeout semantics. Covers AC3, AC5, AC8.
- [x] T6: Extract shared composer presentation and draft mechanics; add per-thread
  drafts and canonical cache updates. Covers AC3, AC4, AC5, AC12.
- [x] T7: Complete first-release browser/transport verification and document the
  GitHub entry points in the feature map. Covers AC1–AC9, AC12, AC13.

### Phase 3 — Start conversations; resolution deferred

- [x] T8: Add line/range gutter composition, revision-aware creation, and
  reconciliation to the canonical thread. Covers AC9, AC10, AC12.
- [ ] T9: Add resolve/reopen mutations and controls with viewer permissions and
  draft-preserving collapse behavior. Covers AC8, AC11.

### Validation

- [x] T10: Extend `tests/runner/test_github_resource.py`,
  `tests/runner/test_github_details_editing.py`, and provider tests for GraphQL
  pagination/partial responses, host/account routing, thread membership, root
  reply IDs, literal Markdown, stale revisions, and unsupported providers. Assert
  one combined PR-data query in the normal enriched read and preserve existing
  metadata/check/comment behavior under permission and schema fallbacks.
- [x] T11: Extend existing server/host permission and resource tests to cover
  read/edit access, runner-offline host fallback, and no second dispatch after a
  timeout. Verify generated OpenAPI drift checks.
- [x] T12: Extend colocated panel, composer, and hook tests for mapping, draft
  isolation, mutation races, partial states, capability gating, and shared
  request counts with both composer and PR panel mounted. Verify inclusion stops
  on leaving Changes, no second timer exists, and omitted threads preserve cached
  discussions. Keep the larger input/error matrix at these layers.
- [x] T13: Reuse the setup and entry-point pattern in
  `tests/e2e_ui/github/test_github_tab.py`. Exercise real `FileDiff` annotations,
  replies, layout switching, scroll/focus, and mobile touch through the workspace
  rail and desktop/mobile composer links. Verify the Canvas entry when enabled
  and any additional embedded surface that exposes this panel; reuse existing
  navigation coverage where it already proves the boundary. Browser stubs prove
  rendering, while runner/host tests prove the GitHub transport contract.
- [x] T-val: Run acceptance criteria verification, relevant Python/Vitest tests,
  frontend type checks/build, applicable pre-commit hooks, and
  `cd web && pnpm exec prettier --check .` before pushing web changes. Capture
  UI evidence outside the tracked tree and attach it to the implementation PR.

### Verification results

Implemented the first release on main `424e96b1c`. Published threads, inline
replies, pagination, revision checks, shared polling and the four browser entry
points are covered. The extension adds compact reply inputs and new line/range
conversations. Resolve/reopen remains deferred; hidden-context threads use the
explicit original-snippet fallback.

- Provider, runner, host fallback, permission, workspace and OpenAPI checks pass.
- Frontend component/hook tests pass, including shared request counts, cursor
  preservation, draft isolation, late writes and collapse/reopen.
- Recorded Chromium journeys pass for Workspace, desktop composer, mobile drawer
  and Canvas, using stubbed GitHub responses and the real diff renderer.
- The combined query was also read against GitHub PR #11546. No live comment was posted.
- Frontend build, TypeScript, Oxlint, Prettier and custom project lint checks pass.
  Ruff and Pyrefly pass after installing the locked Ruff version and cached
  optional telemetry dependencies; no lint rules were disabled.
- Locked Python dependency installation failed on package downloads. Python tests
  used Python 3.12 with dependencies from an existing local environment and imports
  pointed at this checkout; these were not an exact lockfile installation.
- Browser recordings and screenshots stay outside the tracked tree. A desktop
  recording and mobile screenshot are uploaded as GitHub attachments for the PR.

Human verification:

1. Start the checkout with `omnidev` and open its printed UI URL. Use a disposable
   GitHub PR with a published current thread, an old-side thread, an outdated
   thread, and a resolved thread.
2. Open **Workspace → Pull Requests → Changes**. Find the same discussions as
   GitHub, switch unified/split, and check old-side placement and fallback groups.
3. Type a reply, preview it, switch PRs and back, collapse/expand the file, then
   post. Refresh and confirm exactly one reply in the original GitHub thread
   under the displayed account.
4. Push a commit that moves/removes the commented line. Refresh and confirm the
   conversation follows GitHub's current coordinates or becomes explicitly
   outdated, and the existing reply draft survives.
5. Repeat through the desktop composer PR link and mobile drawer. Use keyboard
   and touch, including a long Markdown preview and a short viewport.
6. Click a line number and its gutter “+” to create a separate conversation,
   including on an already-commented line. Drag or Shift-click line numbers on
   one side and choose **Comment on selected lines** to post a range. Verify the
   coordinates on GitHub. Start another draft, push a commit, refresh, and check
   that posting requires reselection while retaining the draft text.
7. In browser DevTools, filter Network to `/resources/github`. While Changes is
   visible, confirm one regular info poll carrying thread inclusion and no
   periodic `/review-threads` request. Switch to Summary and confirm subsequent
   info polls omit thread inclusion. “Load more” may issue one-off page requests.
