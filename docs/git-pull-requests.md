# Pull request providers

The shared pull request panel, composer link, canvas cards, and session tracking
use the provider descriptors described in [Git providers](git-providers.md).
GitHub is the built-in implementation. An installed provider can supply the same
features without changing the dispatcher or frontend registry.

## Implementing the runtime facet

Set the descriptor's `facets.pull_requests` to a module exposing
`PULL_REQUESTS: PullRequestFacet`. The protocol and payload fields are documented
in `omnigent/runner/git_providers/__init__.py`; the GitHub implementation is in
`omnigent/runner/git_providers/github.py`.

`omnigent.runner.pr_resource` owns selection, association persistence, branch
inference, title caching, and routing. A facet implements workspace and linked
request lookup, access checks, titles, changed files, diffs, preferences, and
tool-output recognition. Calls can run concurrently in worker threads. Keep
panel-only dependencies inside the methods that use them: the tool observer
loads facets without opening the panel.

Declare supported controls through `ProviderCapabilities`: account switching,
base remote selection, line counts, and diffs for linked requests outside the
workspace's repository. Return authentication state and actionable host-side
instructions in `auth`. A provider can authenticate without a CLI; the panel
requires a CLI installation only while the provider is also signed out.

Requests route to the selected association's provider. Without an association,
the dispatcher checks the repository-local `omnigent.gitprovider` setting,
then recognized remotes with `origin` first. The first available provider is a
fallback for local repositories or SSH aliases that its CLI may understand.

## Presentation and compatibility

The dispatcher adds `provider_display` to info responses and each association:

```json
{
  "id": "example_forge",
  "display_name": "Example Forge",
  "request_name": "merge request",
  "number_prefix": "!"
}
```

These values come from the descriptor. Unknown frontend providers use a generic
icon and this metadata for their name, request terminology, and number prefix.
Metadata must identify the same provider as the payload or association. GitHub
keeps its existing icon and authentication guidance.

Existing `/resources/github` routes, `session.github.*` object IDs, and browser
cache keys remain stable wire identifiers for all providers. A host that omits
`provider` is treated as GitHub; explicit `provider: null` means no provider is
known. GitHub's legacy top-level `gh_available`, `authenticated`, `accounts`,
and `selected_account` fields remain alongside `auth`; their planned removal
version is **0.19.0**.

## Incomplete and failed responses

An empty collection means the provider successfully found no items. Report
incomplete data explicitly so the panel does not imply a request has no comments,
checks, or changes when its API failed:

| Payload field                     | Meaning and panel behavior                                                                                                             |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `info.warnings: string[]`         | Show lookup problems above the details; keep polling. A null `pr` with warnings is unavailable, rather than a successful empty lookup. |
| `pr.checks.partial: true`         | Qualify check counts as incomplete; keep any loaded checks.                                                                            |
| `pr.checks_supported: false`      | The token cannot read check runs (GitHub refuses them to fine-grained PATs); say so in the Checks area instead of showing counts.      |
| `pr.comments_partial: true`       | Mark comments incomplete; retain loaded comments and display a `+` with their count.                                                   |
| Changed files `has_more: true`    | Mark the file list incomplete.                                                                                                         |
| Changed files `warning: string`   | Show the provider's explanation, including a failed file-list lookup.                                                                  |
| Diff `unavailable_reason: string` | Show an unavailable state instead of rendering an empty patch.                                                                         |
| Diff `message: string`            | Optional user-facing explanation for an unavailable diff.                                                                              |

Use `pr_outside_workspace` only for that specific diff limitation. Other reasons
receive a generic unavailable message unless the provider supplies `message`.
Do not return a truncated patch as a complete diff. Validate requested head/base
revisions before returning file contents so the panel cannot mix revisions.

## Tracking completed tools

The observer asks facets to classify shell commands and MCP tools. Return read
and comment-only shell operations with `tracks=False`; references in their
output must not become session associations. Mutating operations can identify
their target directly or extract canonical request URLs from successful output.
Provider-specific JSON fields belong in `pr_from_object`; common `html_url` and
`url` fields are handled centrally. A failing provider is isolated so it cannot
change a tool's result or prevent another provider from recording requests.

## Server storage

The server owns which requests belong to a session; the runner or host remains
the only forge client. The server keeps each session's requests in one
compressed JSON document on its metadata row
(`omnigent_conversation_metadata.pull_requests`).

**Discovery.** The runner records each request it discovers in the session
registry, which keeps the entries the server has not acknowledged, and sends
them as an `external_pull_requests` session event: right after recording one,
when a turn ends, and when it first serves a session. Entries are cleared only
after a 2xx and retried with backoff otherwise, so a request opened during a
turn reaches the server even if nobody has the session open. Requests the host
finds while serving a read (the branch's request) ride back in the response.
Merging is idempotent and never re-adds an unlinked request.

**Import.** A session is *synced* once the server has merged its host's whole
list, from any proxied pull request response. Until then the host's list still
decides. `GET /v1/sessions/{id}/pull-requests` reports `synced`, and session
list items report `pull_requests_synced`, so clients can tell "not known yet"
from "none". A listed session that isn't synced is imported in the background
when its runner or host tunnel is on the serving replica.

**Serving.** Once synced, the server checks a requested `pr_url` against its
list (400 otherwise) and forwards it with `server_tracked`; with no selection it
sends its first request as `default_pr_url`. The runner or host then serves that
request even if its own registry never recorded it, and the server's list
replaces `prs` in the response. Each entry keeps the request's title, state,
and draft flag with when the server requested that fetch; an older fetch never
overwrites a newer one, including one that changed nothing and skipped its
write. The description, checks, comments, changed files, and diff stay on the
host.

**Freshness.** Summaries carry `state_checked_at`. A listed session whose first
request is open (or unknown) and older than five minutes is refreshed in the
background, at most once per five minutes, when its tunnel is on the serving
replica; clients show older states as stale. After a turn ends the server
refreshes the session once, unless a client's read already covered that turn.

**Limits.** A session keeps at most 50 linked requests and 50 unlinks. Their
identities must fit 12,000 stored bytes, checked when a request is added:
linking at the limit fails with 409 before the host is asked, and reports past
it are ignored. Titles and states fill the rest of the 16,000-byte document and
are trimmed first when it overflows, so refreshing them never removes a link.

**Unlinking.** `DELETE /v1/sessions/{id}/pull-requests?url=` unlinks on the
server whether or not the host is online, and passes the unlink to the runner or
host when one is reachable so its own list stops reporting the request.
`GET /v1/sessions/{id}/pull-requests` pages with `limit` and `after` (the
previous page's `last_id`, a request URL) and never contacts the host. Servers
that store requests report `session_pull_requests` in `/v1/info`; against older
servers the web reads only the live host.

## Verification

### Inline GitHub conversations

GitHub advertises `review_threads`, `review_thread_replies` and
`review_thread_creation`. All default to
false for other providers and older hosts. They use the optional `ReviewThreadsFacet`
without changing the required methods of `PullRequestFacet`.

While **Changes** is visible, `GET review-threads` fetches the first 25 threads
with up to 20 published comments each, then polls every 10 seconds. The existing
PR metadata request continues to use `gh pr view`. Other subtabs and the closed
panel stop thread polling. Replies and new conversations refresh the thread
query immediately. Additional thread and comment pages load on demand.

The thread response carries `data`, `has_more`, `next_cursor`, `head_sha`,
`base_sha` and an optional `warning`. Ordinary polls replace unpaginated comments,
retaining locally posted replies until observed in a read. Explicitly loaded
history is retained and marked for manual reload.

Patch reads verify the expected revisions before and
after fetching; mismatches withhold inline placement. Outdated, hidden-context,
file-level and unavailable-diff threads remain accessible with their original
code context. Resolved threads start collapsed.

Fenced `suggestion` blocks render the referenced lines as removals and the
replacement as additions, with line numbers. They reuse the displayed patch
and its diff renderer for syntax and word-level highlighting.
Outdated or hidden-context threads use the saved snippet labeled **Original
context**. If the complete original range is unavailable, the replacement is
shown without a comparison. Suggestions are display-only and add no requests.

`POST review-threads/{id}/replies` publishes immediately with the selected host
account. The host validates thread membership and replies to its published root
comment. Expanded conversations show a compact **Write a reply…** input. Focus it
to open the Markdown composer; **Cancel** collapses it while retaining the draft.
Thread reads include code excerpts and require the same file-sharing permission
as diff reads. Reply pagination requires session read permission; writes require
edit permission.
Runner and host fallback use the same provider implementation. An uncertain
write is never retried automatically. Drafts and pending/error state stay in
memory per session, PR and thread, including through tab and layout switches.

Select a line number and use the gutter **+** or **Comment on selected lines**
to start a separate conversation, even when that line already has a thread.
The **+** overlays the divider between line numbers and code without widening
the gutter.
Drag line numbers or Shift-click to select a range on the same side within one
diff hunk. `POST review-threads` accepts `pr_url`, `body` and an `anchor` with
`path`, `side`, `line`, optional `start_line`, `head_sha` and `base_sha`.
The provider checks both revisions and publishes a one-thread GraphQL review
with `event: COMMENT` and the displayed commit. Its response identifies the
canonical thread. New drafts retain text and coordinates per session, PR and
file; a revision change requires explicit reselection before posting.

Resolving/reopening threads remains follow-up work.
See the [browser verification recipe](../feature-map/pull-request-conversations.md).

### Provider regression checks

Run the existing GitHub resource and tracking suites together with provider
dispatcher tests, then the shared panel, composer, and canvas frontend tests.
For a manual check, open a GitHub session's Pull Requests tab, select a linked
request, inspect Summary, Comments, and Changes, and confirm the same link
appears in the composer and canvas. Repeat with the installed provider,
including signed-out and partial-data responses. Existing GitHub account and
base-remote selectors must continue to work.
