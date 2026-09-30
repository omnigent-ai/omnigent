# Git providers

The session pull request panel and the PR observer work with any git provider
that implements the layer described here. GitHub and Azure DevOps ship with
Omnigent. This page is for a developer who adds or maintains a provider.
[Azure DevOps pull requests](AZURE_DEVOPS.md) covers the second provider from
the user's side, and its code is the worked example for each step in
[Adding a provider](#adding-a-provider).

## What a provider is

A provider is a descriptor plus optional facets.

The descriptor is a module in `omnigent/git_providers/` that exposes
`PROVIDER`. It declares the provider's `id`, `display_name`, `default_hosts`,
and `facets`, and it recognizes the provider's URLs with `matches_host`,
`parse_remote_url`, and `parse_pr_url`. The `GitProvider` protocol in
[`omnigent/git_providers/__init__.py`](../omnigent/git_providers/__init__.py)
defines the shape. A descriptor imports only the standard library, so any
process can resolve a URL cheaply. `tests/git_providers/test_stdlib_imports.py`
checks that importing the built-in descriptors and loading the registry pulls
in nothing outside the standard library and `omnigent.git_providers`.

A facet is a separate module that the descriptor names in `facets`, a
`FacetModules`. A process imports a facet only when it needs that part of the
provider, with `load_facet(provider_id, kind)`. The facet module exposes an
object named after the kind in upper case, such as `PULL_REQUESTS`.

| Facet kind | Imported by | State |
| --- | --- | --- |
| `pull_requests` | The runner: the PR panel and the PR observer | Implemented |
| `connection` | The server | Later change |
| `credential` | The host and the sandbox | Later change |
| `policy` | The runner policy engine | Later change |

Only `pull_requests` exists so far. `FacetModules` and `load_facet` already
accept the other three kinds, and the built-in providers leave them unset.

## How URLs resolve

`providers()` returns the registered providers in registration order: the
built-ins in `PROVIDER_MODULES` (`github`, then `azure_devops`), then the
modules listed in `OMNIGENT_GIT_PROVIDER_MODULES`, then providers added with
`register_provider()`. The variable holds comma-separated module paths, and
each module exposes `PROVIDER`. A module that cannot be imported, or that has no
`PROVIDER` with a string `id`, is skipped with a warning. A provider whose id is
already registered replaces the earlier one in its original position. The
registry is cached after first use. Tests that change it call
`reset_for_tests()` before and after.

`resolve_remote(url)` and `resolve_pr_url(url)` try providers in two passes.
The first pass covers the providers whose `matches_host` claims the URL's host,
and the second covers all the others. Each pass follows registration order, and
the first provider whose `parse_remote_url` or `parse_pr_url` returns a result
wins. A provider registered later can therefore take a host from an earlier one
by claiming it, and a provider that claims no host still parses the URLs that no
provider claims. `host_of(url)` reads the lower-cased host from an `http`,
`https`, `ssh`, or scp-style URL.

`matches_host` receives an `Instances` object, which reports the hosts
configured for a provider beyond its `default_hosts`. The default,
`EnvInstances`, reads `OMNIGENT_GIT_PROVIDER_<ID>_HOSTS`, a comma-separated host
list in which `<ID>` is the provider id in upper case. Entries are stripped and
lower-cased, and one configured host per provider is the supported case today.
For GitHub the variable is `OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS`, and it lists
GitHub Enterprise hosts. Azure DevOps ignores the setting: the provider serves
only Azure DevOps Services (`dev.azure.com`, its SSH host, and
`*.visualstudio.com`), and Azure DevOps Server is not supported.

Give a provider an id of lowercase letters, digits, and underscores so that the
variable name is valid. The registry does not check this.

A parsed remote is `ParsedRemote(provider, host, repository)`. A parsed pull
request is `ParsedPullRequest(provider, host, repository, number, url)`, where
`url` is the canonical URL. `SessionPrRegistry` keys a session's PRs by that URL
and parses the URL again when it records one, so the canonical URL has to parse
back to itself.

`tests/runner/test_git_providers_generality.py` registers a fake provider that
lives outside Omnigent and drives the panel and the observer with it.

### Built-in host rules

GitHub:

- A pull request URL parses as GitHub on any host:
  `https://{host}/{owner}/{repo}/pull/{number}`, optionally followed by
  `/files`, `/commits`, or `/checks`. A URL with credentials or a port is
  rejected.
- A remote is GitHub when its host is `github.com`, the value of `GH_HOST`, a
  host in `OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS`, or a host that the `gh` CLI
  lists in its `hosts.yml`. This keeps GitHub Enterprise remotes on GitHub for
  anyone who signed in to them with `gh`.

Azure DevOps claims `dev.azure.com`, `ssh.dev.azure.com`, and every
`*.visualstudio.com` host. It does not use configured hosts.
[Azure DevOps pull requests](AZURE_DEVOPS.md) lists the URL forms it accepts.

## How the panel picks a provider

`omnigent/runner/pr_resource.py` is the panel's dispatcher. Its
`resolve_provider` returns a `ProviderResolution` (`provider`, `remote_host`,
and `unclaimed`) from the first of these rules that applies:

1. The provider of the selected PR, or of the session's first tracked PR. Each
   tracked PR stores its provider id. A registry file written before providers
   existed has none and loads as `github`.
2. The value of `omnigent.gitprovider` in the repository's own git config,
   lower-cased. Set it with `git config --local omnigent.gitprovider <id>`.
   Global and system config are ignored, so one setting cannot re-route every
   workspace.
3. The first remote that `resolve_remote` assigns to a provider with a
   `pull_requests` facet. The dispatcher reads each remote's first configured
   URL, with `origin` first and the other remotes in config order.
4. The first registered provider. Its CLI may still resolve a remote that no
   descriptor recognizes, such as an ssh host alias. When the workspace has a
   network remote, the resolution is marked `unclaimed`.

The info payload is `available: false` with `reason: "unsupported_remote"` and a
`remote_host` in two cases:

- The resolved provider has no `pull_requests` facet. This happens when
  `omnigent.gitprovider` names an unknown provider or one without the facet,
  when no provider is registered, and for a tracked PR whose provider has no
  facet. `remote_host` is the tracked PR's host, else the host of the
  workspace's first network remote, else an empty string.
- The resolution is `unclaimed` and the first provider's info shows that it
  cannot serve the workspace. That means its info is available, it is signed
  in, its CLI (if it has one) is installed, it found neither a PR nor a
  repository, and the panel has no other account to offer (the provider lacks
  `account_switching`, or `auth.accounts` has at most one entry). If the CLI is
  missing or signed out, the panel shows that provider's install or sign-in
  guidance instead.

Attaching a PR whose provider has no facet fails with a `ValueError` that reads
"`<name>` pull requests are not supported".

## The pull request facet

The `PullRequestFacet` protocol in
[`omnigent/runner/git_providers/__init__.py`](../omnigent/runner/git_providers/__init__.py)
is the contract. Its module docstring and method docstrings hold the full
signatures and the payload rules. The dispatcher keeps the provider-neutral
steps: the selected PR, tracked PRs and their cached titles, branch inference,
attach and remove, and routing preferences. It calls the facet for every step
that talks to a forge. The runner routes and the host's fallback ops both go
through the dispatcher.

| Method | What it does |
| --- | --- |
| `capabilities` | The panel features the provider supports, as a `ProviderCapabilities`. |
| `workspace_info(root)` | The info payload for the workspace's current branch and its PR. Outside a git checkout it returns `available: false` with `reason: "not_a_git_repo"`. |
| `reference_info(root, reference)` | The info payload for one tracked PR, independent of the checkout. |
| `titles_available(root)` | Whether `pr_title` can run now, for example because the CLI is installed or a credential exists. When false, the dispatcher skips this provider's titles. |
| `pr_title(root, reference, deadline)` | One PR's title as `(title, timed_out)`, giving up at `deadline`. Never raises. |
| `verify_accessible(root, reference)` | Raises `ValueError` with a user-facing message unless the host's credentials can read the PR. Runs before a PR is attached. |
| `on_inferred_pr(root, reference)` | Runs after the dispatcher associates a PR that it inferred from the workspace branch. GitHub copies the workspace's account preference to the PR here. |
| `changed_files(root, reference)` | The PR's changed files. |
| `pr_diff(root, reference)` | The whole PR as one unified diff. |
| `file_diff(root, reference, path, ...)` | One file's full content before and after the change. |
| `set_preference(root, reference, *, account, remote)` | Saves the user's account or base remote choice. |
| `shell_pr_operations(segments)` | Observer hook: one `ShellPrOp` for each shell segment that runs the provider's PR command. |
| `pr_from_object(obj)` | Observer hook: the PR that provider-specific fields of a JSON object in shell output name. |
| `mcp_prs(tool_name, arguments, result)` | Observer hook: the PRs from a successful call of one of the provider's MCP tools. |

`root` is the absolute path of the session workspace. A `reference` of `None`
means the PR of the workspace's current branch. Methods block and can run in
worker threads at the same time. Report a forge or credential failure in the
result, for example `pr: None` with `auth.authenticated: False`, or an empty
file list. Raise `ValueError` only with a message meant for the user, because
the routes return it as HTTP 400.

The observer loads every provider's facet each time a tool call completes. A
facet module therefore imports its panel-only dependencies inside the methods
that use them. The GitHub facet imports `omnigent.runner.github_resource` inside
its methods, and the Azure DevOps facet imports `httpx` and
`omnigent.runner.azure_devops_client` inside its functions.

### Observer hooks

`omnigent/runner/pr_observer.py` finds the PRs that a session's agent creates or
changes in completed tool calls. It applies the provider-neutral attribution
rules and asks every facet, in registration order, for the provider-specific
parts. The hooks run after every tool call and must not call the forge.
`tests/runner/test_pr_observer_azure_devops.py` fails when a request reaches the
HTTP transport. A facet module that fails to import, or a hook that raises, is
logged and skipped, and the other providers still run.

For a shell tool call, the observer splits the command on `;`, `&`, `|`, and
newlines, unwraps nested shell `-c` strings, and passes the pieces to each facet
as `ShellSegment` values. `raw_tokens` is the segment as lexed, with leading
environment assignments such as `GH_HOST=example.com` and command wrappers.
`invocation_tokens` is the real command and its arguments, and is never empty. A
command that contains `||`, or that cannot be lexed, yields no segments.
`shell_pr_operations` returns one `ShellPrOp` for each segment that runs the
provider's PR command, in order, reads included.

| `ShellPrOp` field | Meaning |
| --- | --- |
| `tracks` | The command changes the PR: create, edit, merge, close, or a review that approves or requests changes. Reads and comment-only commands do not track. |
| `creates` | The command creates a PR. Implies `tracks`. |
| `target` | The `PullRequestRef` that the command's arguments name, or `None` when they name none, as on create or for the current branch's PR. |
| `content_only` | The command prints only PR content (body, title, or diff), so URLs in its output do not identify the PR. |

The observer records PRs only when at least one op tracks. The PRs count as
`created` when every tracking op creates, and as `worked_on` otherwise. It
records the `target` of each tracking op. It also reads PRs from the shared
output (JSON objects, and lines that hold only a PR URL), but only when every
recognized op tracks and, when there is a single op, that op is not
`content_only`. Report a read command as an op with `tracks` false. A read next
to a write makes the shared output ambiguous, and the observer then ignores it.

For each JSON object in that output, the observer reads the generic `html_url`
and `url` fields first, then calls each facet's `pr_from_object` for fields
specific to the provider, such as the `pullRequestId` and `repository` that
`az repos pr` prints.

For a tool call that is not a shell call, such as an MCP tool, the observer
calls each facet's `mcp_prs`. The first answer that is not `None` wins, and
duplicate URLs are removed. `mcp_prs` returns `(references, created)`, where
`created` is true when the tool created the PRs and `references` can be empty,
or `None` when the tool is not one of the provider's PR tools.

`omnigent/runner/git_providers/tool_output.py` has the helpers for these hooks.
It imports only the PR reference model, because the observer imports it on every
tool completion.

| Helper | Returns |
| --- | --- |
| `pr_reference(value)` | The `PullRequestRef` that a URL string names, ignoring trailing punctuation, or `None`. Only a URL that a registered descriptor parses names a PR. |
| `result_objects(result)` | Every JSON object in a tool result, envelope objects included. JSON in text counts only when it ends its line. Only envelope fields such as `content`, `stdout`, and `result` are unwrapped, so JSON quoted in a body or description field is not. |
| `output_text(result)` | The plain-text lines of a tool result, without the JSON that ends its line. |

The built-in providers keep their rules in `github_observer.py` and
`azure_devops_observer.py` next to the facets, and the facet methods call them.

## The payload

Every provider returns the same payload shapes to the panel. The contract is in
the module docstring of `omnigent/runner/git_providers/__init__.py`, which also
defines the object id constants (`INFO_OBJECT`, `CHANGED_FILE_OBJECT`,
`FILE_DIFF_OBJECT`, and `PR_DIFF_OBJECT`) and `unsupported_remote_info(host)`.
That function builds the payload for an unsupported remote: `available: false`,
`reason: "unsupported_remote"`, `remote_host`, and null `provider`, `auth`,
`capabilities`, `repo`, and `pr`.

The info payload is a `session.github.info` object. Every provider fills these
fields:

- `available`, with `reason` when it is false.
- `branch`, `base_ref`, and `repo`, which is `{"name_with_owner": ...}`.
- `pr`, the PR being shown.
- `selected_pr_url`, for a tracked PR.

`pr` has `number`, `url`, `title`, `state` (`OPEN`, `MERGED`, or `CLOSED`),
`is_draft`, `author`, `base_ref`, `head_ref`, `checks`, `body`, and `comments`,
and `head_sha` and `base_sha` when known. `url` is a URL that the provider's
descriptor parses. `checks` has `passing`, `failing`, `pending`, `total`, and
`runs`, a list of `{name, bucket, url}`. A comment has `author`, `body`,
`created_at`, and `url`. The panel sends `head_sha` and `base_sha` back with a
file-diff request so that `file_diff` can reject a PR whose head has moved. When
it runs for a session, the dispatcher's `pr_info` adds `prs`, the session's
tracked PRs with their cached titles, and `tracking_available`.

The provider layer added these fields, which a host that predates it does not
send:

- `provider`: the provider id, for example `github`. It is null for an
  unsupported remote.
- `auth`: whether the provider can reach the PR. It has `authenticated`;
  `hint`, a short next step for the user when `authenticated` is false, such as
  a sign-in command; `cli`, which is `{name, available}`, or null for a provider
  that runs no CLI; `accounts`, the accounts the user can choose among, or null;
  and `selected_account`, the account the provider acts as, when known. It is
  null when `available` is false.
- `capabilities`: the four flags below, from `ProviderCapabilities.to_json()`.
  It is null for an unsupported remote.
- `remote_host`: sent only with `reason: "unsupported_remote"`.
- `provider` on each association in `prs`.
- `author_id` on `pr` and on each comment: the author's stable id on the
  provider. Optional, may be null.

| Capability | Meaning | Read by |
| --- | --- | --- |
| `account_switching` | The user can choose among several signed-in CLI accounts. `auth.accounts` lists them and `set_preference` accepts `account`. | The dispatcher and the web panel's account selector |
| `base_remote_selection` | The user can choose the base remote, which `set_preference` accepts as `remote`. | The dispatcher |
| `line_counts` | Changed files carry line counts. Without it, `lines_added` and `lines_removed` are null. | Informational |
| `linked_pr_diff` | A PR linked from outside the workspace's repository still has a diff. Without it, `pr_diff` returns `unavailable_reason: "pr_outside_workspace"` for that PR. | Informational |

GitHub sets all four. Azure DevOps sets none. The dispatcher passes a preference
choice to the facet only when the matching capability is set. The web panel
offers its account selector only when `account_switching` is set and
`auth.accounts` has more than one entry.

`line_counts` and `linked_pr_diff` are informational. The facet honors them
itself, and the web panel relies on the data at runtime: null line counts and
`unavailable_reason`. Of the four flags, the web panel reads only
`account_switching`, and the dispatcher reads `account_switching` and
`base_remote_selection`. Other clients of the info payload can use all four.

GitHub also sends the legacy top-level fields `gh_available`, `authenticated`,
`accounts`, and `selected_account`. `auth` supersedes them. They are deprecated
and will be removed in 0.19.0, together with `legacyPullRequestAuth` in
`web/src/hooks/usePullRequests.ts`. Other providers omit them.

`changed_files` returns `{"object": "list", "data": [...], "has_more": false}`.
`has_more` is true when the list is incomplete, as when the Azure DevOps facet
runs out of time between pages of changes.
Each item is a `session.github.changed_file` with `path`, `name`, `status`
(`created`, `modified`, `deleted`, or `renamed`), `lines_added`, and
`lines_removed`. The two counts are null for a provider without `line_counts`.
The list is empty when no PR resolves.

`pr_diff` returns a `session.github.pr_diff` with `patch`, the whole PR as one
unified diff. `patch` is empty when no PR resolves. A provider without
`linked_pr_diff` also returns `unavailable_reason: "pr_outside_workspace"` for a
PR from outside the workspace's repository, and the panel then shows a message
instead of a diff.

`file_diff` returns a `session.github.file_diff` with `path`, `before`, and
`after`: the file's full content at the PR's merge base and at its head.
`before` is null for an added file and `after` is null for a deleted one.

## Frontend

`web/src/lib/gitProviders.ts` holds the name, icon, and wording that the panel
shows for each provider. `GIT_PROVIDERS` maps a provider id to a
`GitProviderCopy`, and `gitProviderCopy(id, authHint)` picks the entry to use.

| `GitProviderCopy` field | Use |
| --- | --- |
| `id` | The payload's `provider` id. |
| `label` | The display name, such as "GitHub". |
| `Icon` | The provider's glyph. |
| `prNumberPrefix` | Shown before a PR number: `#` for GitHub, `!` for Azure DevOps. |
| `prUrlPlaceholder` | The example URL in the link-a-PR input. |
| `defaultHost` | The host that PR labels leave out. `null` shows every host. |
| `authHint` | How to sign in on the host. Text in backticks renders as code. |
| `cliLabel` | The name of the provider's CLI, as in "Install the GitHub CLI". |
| `signInWithoutCli` | Optional. The provider can sign in without its CLI, for example with a token, so a missing CLI also shows `authHint`. |
| `repoUnresolvedHint` | Optional. The hint for an upstream repository that cannot be reached. `authHint` when absent. |

`gitProviderCopy` falls back in this order:

- An undefined or empty id returns the GitHub entry, because a host that
  predates the `provider` field serves GitHub.
- `null` returns neutral copy that names no provider. Its label is "Pull
  Requests". The panel uses it while no provider is known, for example before
  the payload loads or for an unsupported remote.
- An id that is not in `GIT_PROVIDERS` returns generic copy: the id as the
  label, `#` as the PR prefix, no default host, and `<id> CLI` as the CLI label.
  The sign-in hint is the `authHint` argument when given, which the panel fills
  from the payload's `auth.hint`, else "Sign in to `<id>` on the host." Known
  providers ignore the argument.

A provider works without an entry, with the generic copy.

`normalizePullRequestInfo` in `web/src/hooks/usePullRequests.ts` fills
`provider`, `auth`, and `capabilities` for a payload from a host that predates
them. A missing `provider` becomes `github`, and an explicit `null` stays
`null`. A missing `auth` is built from the legacy GitHub fields, and missing
`capabilities` become GitHub's set. Applying it twice gives the same result.

## Stable wire identifiers

Other processes, other versions of this code, saved browser state, and files on
disk refer to the identifiers below by name. They keep the `github` name for
every provider, including Azure DevOps. Renaming one breaks them.

| Identifier | Where it appears |
| --- | --- |
| `/v1/sessions/{id}/resources/github` and the routes under it: `/changes`, `/diff`, `/diff/{path}`, `/prs`, `/preferences` | `omnigent/server/routes/sessions/routes_resources.py`, `omnigent/runner/app.py`, `web/src/hooks/usePullRequests.ts` |
| Host ops `github_info`, `github_changes`, `github_diff`, `github_pr_diff`, `github_set_preference`, `github_prs_update` | `omnigent/host/connect.py`, `omnigent/host/frames.py`, `omnigent/server/routes/_host_filesystem.py` |
| `object` values `session.github.info`, `session.github.changed_file`, `session.github.file_diff`, `session.github.pr_diff` | `omnigent/runner/git_providers/__init__.py` |
| Rail tab id `github` | `web/src/shell/railTabs.ts`. Saved in localStorage by `web/src/lib/sessionWorkspaceState.ts` and `web/src/lib/workspaceTabPreferences.ts`. |
| Registry path `data_dir()/github/session-prs` | `omnigent/runner/session_prs.py` |
| Query keys `github-info`, `github-changed-files`, `github-pr-diff` | `web/src/hooks/usePullRequests.ts`. Also read by `web/src/canvas/pullRequests.ts` and `web/src/extensions/services/useExtensionHostServices.ts`. |
| `testId="github-panel-drawer"` | `web/src/shell/AppShell.tsx`. The e2e tests select it. |
| `componentId="github.panel.tabs"` | `web/src/shell/PullRequestPanel.tsx`. It is the analytics id of the panel's tabs. |

## Adding a provider

Each step has a counterpart in the Azure DevOps provider.

1. Write the descriptor. Add `omnigent/git_providers/<id>.py` that exposes
   `PROVIDER`, as `omnigent/git_providers/github.py` and
   `omnigent/git_providers/azure_devops.py` do, and add its module path to
   `PROVIDER_MODULES` in `omnigent/git_providers/__init__.py`. A provider that
   lives outside the repository goes in `OMNIGENT_GIT_PROVIDER_MODULES`
   instead. Name the facet modules in `facets`. Import only the standard library
   and `omnigent.git_providers`.
2. Test the URL parsing in `tests/git_providers/test_<id>_descriptor.py`: the
   remote forms, the PR URL forms, the URLs that must be rejected, host matching
   (including configured hosts, if the provider uses them), and the canonical PR
   URL, which has to parse back to itself. `test_azure_devops_descriptor.py`
   also checks that `SessionPrRegistry` records URLs that differ in case or host
   as one entry.
3. Write a client module if the provider needs one, such as
   `omnigent/runner/azure_devops_client.py`. It can import its HTTP library at
   the top, because only the facet's methods import the client. Test it against
   a mock transport, as `tests/runner/test_azure_devops_client.py` does.
4. Write the `pull_requests` facet in `omnigent/runner/git_providers/<id>.py`,
   exposing `PULL_REQUESTS`. Implement every `PullRequestFacet` member, declare
   the `ProviderCapabilities`, and return the neutral and additive payload
   fields. Import panel-only dependencies, the client included, inside the
   methods that use them, because the observer loads every facet. For local git
   reads, use `omnigent/runner/git_providers/local_git.py`: the workspace's
   remote URLs, the diff base of the checkout, and a file at a revision. Each of
   its functions takes the facet's own git runner. Test the facet
   directly (`tests/runner/test_azure_devops_provider.py`) and through the
   dispatcher (`tests/runner/test_pr_resource_azure_devops.py`).
5. Write the observer hooks `shell_pr_operations`, `pr_from_object`, and
   `mcp_prs`. Put the rules in `omnigent/runner/git_providers/<id>_observer.py`
   so the facet stays small. Report read commands as ops with `tracks` false,
   and return `None` from `mcp_prs` for tools that are not the provider's. Test
   the rules (`tests/runner/test_azure_devops_observer.py`) and the result
   through `extract_prs` (`tests/runner/test_pr_observer_azure_devops.py`).
6. Add the provider to `GIT_PROVIDERS` in `web/src/lib/gitProviders.ts`, with
   its `id` equal to the provider id, and cover it in
   `web/src/lib/gitProviders.test.ts`.
7. Add an e2e fixture in `tests/e2e_ui/<id>/`.
   `tests/e2e_ui/azure_devops/test_azure_devops_tab.py` stubs the
   `/resources/github*` routes with `page.route` and canned payloads, so the
   panel runs without a real forge.
8. When the connection, credential, and policy facets exist, add a module for
   each and name it in `FacetModules`.

Run the provider layer's Python and web tests with:

```bash
uv run --no-sync pytest tests/git_providers tests/runner/test_git_provider_protocol.py tests/runner/test_pr_resource.py
pnpm --dir web exec vitest run src/lib/gitProviders.test.ts
```
