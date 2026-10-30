# Pull request conversations

Read, reply to and start GitHub review conversations beside the code in a pull
request's Changes subtab. Save local session comments alongside them and prepare
a batch for the agent in chat. Top-level PR comments remain in Comments.

## Sub-features

- `inline-threads`: Published conversations on old/new lines, both diff layouts,
  separate conversations at the same line and multiline range labels.
- `suggestions`: Fenced suggestions show referenced lines as removals and the
  replacement as additions, with syntax and word-level highlighting.
  Saved snippets are labeled as original context;
  unavailable ranges show the replacement without a comparison.
- `inline-reply`: Compact reply inputs expand on focus; Cancel collapses the
  composer and retains the draft. Markdown preview, account attribution and
  immediate posting remain available in the expanded composer.
- `new-conversation`: Gutter plus and line-selection actions for a single line or
  same-side range; independent conversations on lines with existing threads.
  Selection follows the drag, with the sidebar's theme tint and an accent gutter.
  The plus hides while the composer is open; its top-right X closes it.
- `history`: On-demand pages, incomplete/error states, resolved collapse,
  unresolved navigation and original context for unplaceable conversations.
- `local-review`: Save session-only comments on either diff side, edit/delete or
  mark them addressed. **Add to chat · N** appends all open local comments for
  this PR to the session composer, preserving existing text and attachments.
  The message contains comment IDs, bodies, paths, lines and commit revisions;
  the user reviews it and sends separately. Mobile dismisses the PR drawer.
- `thread-refresh`: Threads fetch separately and poll while Changes is open;
  other subtabs, the PR list and the collapsed rail stop requesting them.
  A compact **Loading review threads** spinner beside the layout toggle indicates
  an in-flight read. Conversation navigation shares the diff toolbar.

## How to get to it (user POV)

- `rail`: Open a session, expand Workspace, choose Pull Requests, select a request
  from the list, then open Changes.
- `composer-desktop`: Click the desktop composer's PR link, select a request,
  then open Changes.
- `composer-mobile`: Tap the composer's PR link, select a request in the drawer,
  then open Changes.
- `canvas`: Open a conversation beside Canvas, click its composer PR link,
  select a request, then open Changes.

## Driving it with the repro environment

Preconditions: Build this checkout and launch the isolated instance with
[Verify Omnigent](skills/verify-omnigent/SKILL.md). No live GitHub credentials
are needed for the browser journeys; their PR responses are stubbed.

Run `tests/e2e_ui/github/test_github_tab.py::test_reply_to_inline_review_conversation`
through the instance with recording enabled. Its four variants cover `rail`,
`composer-desktop`, `composer-mobile` and `canvas`: a conversation renders inside
the real diff, its draft survives the PR list, tabs and layout switching, preview
renders Markdown, and exactly one reply remains after refresh. Each variant also
creates a separate conversation on an occupied new-side line and a range on the
old side, verifying coordinates, placement and deduplication after refresh.
It checks focus-to-expand, Cancel and keyboard reopening, collapse after posting,
and the gutter plus centered over the line-number/code divider in both layouts.
Suggestion assertions verify the before/after lines and numbers, highlighting, mobile sizing,
and original-context labeling on outdated threads. Component tests cover empty
replacements, ordinary Markdown/code and unavailable context; mapping tests
cover side/range selection, partial patches and historical coordinates.

The existing
`tests/e2e_ui/github/test_github_tab.py::test_post_top_level_pr_comment`
covers the separate Comments composer. Component and runner tests carry the
pagination, old-side placement, revision, permission and failure matrices.

For local review, use any of the four entry points above, select a diff line,
choose **Local comment**, and save comments on two files. Reload and confirm
both remain. Click **Add to chat · 2** with an existing chat draft: its text and
attachments should remain, both comment references should be appended, and no
message should be sent yet. Click again to check deduplication. Mark a comment
addressed and confirm the count decreases. Switch PRs and sessions to check
isolation. After a new commit, verify earlier comments retain their old line
references instead of moving onto unrelated lines. Repeat in split/unified
layouts and mobile (the drawer should close on Add to chat).

The same four browser variants also save local comments on both diff sides,
retain the Local destination through tab switching, cross-check session storage
through the REST API, and append both comments to an existing chat draft. The
mobile variant verifies that the drawer closes. Provider write counts stay
unchanged during local review.

The local-review component flow is covered by
`web/src/shell/PullRequestPanel.test.tsx`, composer preservation by
`web/src/pages/ChatPage.composer.test.tsx`, and persistence/formatting by
`tests/server/routes/test_comments_crud.py::test_local_pr_comment_round_trip`.
These checks do not replace a recorded drive of the four entry points.

For a live check, use a disposable GitHub PR with current, old-side, resolved
and outdated threads. Open Changes, compare both layouts to GitHub, focus the
reply input and type a draft. Cancel, reopen and confirm the text survives.
Preview the reply, switch tabs and back, post, then refresh. Confirm one reply
under the displayed account on GitHub. Move the commented line in a new commit
and confirm the discussion remains accessible. Click a line number and its plus
to start another conversation; drag line numbers or Shift-click to select a range
and click its gutter plus. Confirm the range on GitHub. In browser Network, the existing
thread requests should poll only while Changes is visible.
For an existing comment with a fenced `suggestion`, compare its red removal and
green addition rows to GitHub, including a multiline replacement. Expand an
outdated thread and confirm the suggestion uses its labeled original context.

## Gotchas

- Browser stubs prove UI behavior; runner/host tests prove provider transport.
  They do not constitute a live GitHub write test.
- Local review comments reuse session comment storage, with immutable PR
  coordinates separate from Files character offsets. They are visible to session
  collaborators, persist across reloads, and are never posted to GitHub.
  The batch is scoped to the current PR; Files comments are not included.
  GitHub review replies still publish immediately, without a draft review.
- Older history refreshes on demand; a first-page poll does not refresh every page.
- Hidden-context and outdated conversations use an explicit original snippet;
  they are never attached to an unrelated current line.
- Canvas requires its release feature. Mobile uses the drawer instead of Workspace.
- Drafts survive navigation in this app instance, but not a browser reload.
- New ranges must stay within one displayed diff hunk and on one side. A changed
  base/head revision preserves the draft but requires selecting its lines again.
- New conversations publish immediately as a submitted comment review. They do
  not append to an existing thread or submit an existing pending review.
- Suggestion diffs reuse loaded patches and saved snippets; applying a suggestion
  is not supported.
