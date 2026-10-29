# Pull request conversations

Read, reply to and start GitHub review conversations beside the code in a pull
request's Changes subtab. Top-level comments remain in Comments.

## Sub-features

- `inline-threads`: Published conversations on old/new lines, both diff layouts,
  separate conversations at the same line and multiline range labels.
- `inline-reply`: Always-visible composers in expanded threads, Markdown preview,
  account attribution, immediate posting and drafts retained across navigation.
- `new-conversation`: Gutter plus and line-selection actions for a single line or
  same-side range; independent conversations on lines with existing threads.
- `history`: On-demand pages, incomplete/error states, resolved collapse,
  unresolved navigation and original context for unplaceable conversations.
- `shared-refresh`: Threads join the existing info poll while Changes is open;
  other subtabs, the PR list and the collapsed rail stop requesting them.

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

The existing
`tests/e2e_ui/github/test_github_tab.py::test_post_top_level_pr_comment`
covers the separate Comments composer. Component and runner tests carry the
pagination, old-side placement, revision, permission and failure matrices.

For a live check, use a disposable GitHub PR with current, old-side, resolved
and outdated threads. Open Changes, compare both layouts to GitHub, type and
preview a reply, switch tabs and back, post, then refresh. Confirm one reply
under the displayed account on GitHub. Move the commented line in a new commit
and confirm the discussion remains accessible. Click a line number and its plus
to start another conversation; drag line numbers or Shift-click to select a range
and choose **Comment on selected lines**. Confirm the range on GitHub. In browser Network, the existing
info requests should include threads only while Changes is visible.

## Gotchas

- Browser stubs prove UI behavior; runner/host tests prove provider transport.
  They do not constitute a live GitHub write test.
- Session file comments addressed to the agent and PR top-level comments are
  separate features. Review replies publish immediately, without a draft review.
- Older history refreshes on demand; a first-page poll does not refresh every page.
- Hidden-context and outdated conversations use an explicit original snippet;
  they are never attached to an unrelated current line.
- Canvas requires its release feature. Mobile uses the drawer instead of Workspace.
- Drafts survive navigation in this app instance, but not a browser reload.
- New ranges must stay within one displayed diff hunk and on one side. A changed
  base/head revision preserves the draft but requires selecting its lines again.
- New conversations publish immediately as a submitted comment review. They do
  not append to an existing thread or submit an existing pending review.
