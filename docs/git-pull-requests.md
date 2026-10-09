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

## Verification

Run the existing GitHub resource and tracking suites together with provider
dispatcher tests, then the shared panel, composer, and canvas frontend tests.
For a manual check, open a GitHub session's Pull Requests tab, select a linked
request, inspect Summary and Changes, and confirm the same link appears in the
composer and canvas. Repeat with the installed provider, including signed-out
and partial-data responses. Existing GitHub account and base-remote selectors
must continue to work.
