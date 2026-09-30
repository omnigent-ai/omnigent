# Git providers

A git provider plugs into Omnigent with a descriptor and up to four facets: pull
requests, connection, credential, and policy. GitHub and Azure DevOps ship with
Omnigent. This page is for a developer who adds or maintains a provider.
[Azure DevOps pull requests](AZURE_DEVOPS.md) covers the second provider from
the user's side, and its code is the worked example for the pull request steps
in [Adding a provider](#adding-a-provider). GitHub is the worked example for the
connection, credential, and policy steps.

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

| Facet kind | Exposes | Imported by | Built-in providers |
| --- | --- | --- | --- |
| `pull_requests` | `PULL_REQUESTS`, a `PullRequestFacet` | The runner: the PR panel and the PR observer | GitHub, Azure DevOps |
| `connection` | `CONNECTION`, a `ConnectionFacet` | The server | GitHub |
| `credential` | `CREDENTIAL`, a `CredentialFacet` | The host and the sandbox | GitHub |
| `policy` | `POLICY`, the registry handler path of the provider's policy | The runner policy engine | GitHub |

`load_facet` returns `None` for a facet that the descriptor does not name, so a
provider implements only the facets it needs. Azure DevOps has `pull_requests`
only. [Providers in `/v1/info`](#providers-in-v1info) shows what each provider
offers on a server.

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
`https`, `ssh`, or scp-style URL. A descriptor that raises is logged and
skipped, so the other providers still resolve. `resolve_remote` returns nothing
for a URL with a backslash, because `urlsplit` and git can read its host
differently.

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
and the new-chat repository picker show for each provider. `GIT_PROVIDERS` maps
a provider id to a `GitProviderCopy`, and `gitProviderCopy(id, authHint)` picks
the entry to use.

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
| `cloneUrlFor` | Optional. The clone URL of a repo that the provider's connection lists, for a listing that carries no `clone_url`. GitHub returns `https://github.com/{fullName}.git`. |

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

## The connection facet

The connection facet is the server's half of a provider's per-user connection:
the routes that link a user's account, the store that keeps the link, and the
credential that the server vends to that user's sandboxes. The
`ConnectionFacet` protocol in
[`omnigent/server/git_providers/__init__.py`](../omnigent/server/git_providers/__init__.py)
is the contract, and `omnigent/server/git_providers/github.py` is the example.
The config, store, and client are the provider's own types. The server keeps
them on `app.state` and passes them back to the facet.

| Member | What it does |
| --- | --- |
| `repo_browser` | Whether the router lists the user's repositories for the new-chat picker. |
| `config_from_env()` | The provider's config from the environment, or `None` when it is unset. Raises when the environment configures the provider incorrectly. |
| `make_store(db_uri, cipher)` | The per-user connection store over the shared credential store. |
| `make_client(config)` | The provider API client that the router and the credential resolver share. |
| `make_router(config, store, *, auth_provider, client)` | The provider's `/connections/<id>/*` routes, mounted under `/v1`. |
| `resolve_credential(user_id, *, store, client)` | The credential to vend for the user, or `None` when the user has not linked the provider. |

`connection_facets()` yields `(provider_id, facet)` for each provider whose
connection facet loads, in registration order. `connections_from_env(db_uri)`
builds `{provider_id: (config, store)}` for the providers that the environment
configures. `omnigent server` and the Docker entrypoint
(`deploy/docker/entrypoint.py`) pass that mapping to
`create_app(connections=...)`. `connection_providers()` in
`omnigent/server/connections_registry.py` turns the loaded facets into
`ConnectionProvider` entries, followed by Databricks, and `create_app` iterates
them.

A facet module that fails to import, or that does not implement
`ConnectionFacet`, is logged and skipped, so one broken provider does not stop
the server. The log names the provider and the exception type, not its text,
which can quote configuration. An error raised by `config_from_env` or
`make_store` stops startup, so a misconfigured provider is never disabled
silently. A configured provider whose credential store has no cipher stays in
the mapping with a `None` store, so its connection is disabled, and the server
logs that a credential cipher (`OMNIGENT_CREDENTIAL_KMS_KEY_ID` or
`OMNIGENT_CREDENTIAL_VAULT_KEY`) enables it. The cipher is built once,
and only when a provider is configured.

`create_app` enables a provider when both its config and its store are present.
It then sets `app.state.<id>_config`, `app.state.<id>_store`, and
`app.state.<id>_client`, mounts the provider's router under `/v1`, and lists the
provider in `enabled_connections`. A provider that lacks either one gets `None`
for all three and no routes.

The `github_config` and `github_store` arguments of `create_app` are deprecated
and will be removed in 0.19.0. They emit a `DeprecationWarning` and are merged
into `connections["github"]`, and naming `"github"` in both places raises
`ValueError`. Pass `connections={"github": (config, store)}` instead. The GitHub
facet reads the `OMNIGENT_GITHUB_APP_*` variables, and
[GitHub App setup](GITHUB_APP_SETUP.md) covers its configuration.

## The credential facet

A managed sandbox gets the session owner's provider credential from the server
when it needs one. The server half is the broker route, and the sandbox half is
the git credential helper, which a credential facet feeds.

### The broker route

`GET /v1/hosts/{host_id}/credentials/{provider}`, in
`omnigent/server/routes/host_credentials.py`, vends the session owner's
credential for one provider. The caller sends the host's launch token in the
`X-Omnigent-Host-Token` header, the same channel that the host tunnel uses, and
the server resolves the token to the session owner. `{provider}` is the id of a
connection provider, Databricks included, and the route calls that provider's
`resolve_credential`. The response is `Cache-Control: no-store`.

- `401` when the token is missing or does not resolve.
- `404` with the body `{"detail": "unknown credential provider"}` when the
  server has no connection for the provider.
- `{"connected": false}` when the owner has not linked the provider, or when
  `resolve_credential` raises.
- `{"connected": true, "owner": ...}` plus the provider's payload otherwise.
  `owner` is the session owner's user id.

The payload of the GitHub facet has these fields:

| Field | Meaning |
| --- | --- |
| `token` | The credential. Every facet sends it. |
| `username` | The git username that goes with the token, `x-access-token` for GitHub. Optional. |
| `login` | The owner's GitHub login. The helper uses it as the commit author's name and as gh's `user`. |
| `expires_at` | The token's expiry in epoch seconds, or null when it does not expire. Optional. |
| `hosts` | The lower-cased git host names that the token authenticates to, with no scheme or port. GitHub sends `["github.com"]`. Optional. |

The server resolves the credential again on each request, and it stops vending
when the launch token expires or the host is deleted. A token that it already
vended stays valid at the provider for its own lifetime, so the trust boundary is
the sandbox. [The credential store design](../designs/CREDENTIAL_STORE.md)
covers the threat model.

### The helper

The helper is `python -m omnigent.git_credential --server <url> --host-id <id>
--host-token <token>`, from `omnigent/git_credential/__init__.py`. git runs it as
`credential.https://<host>.helper` with `get` and the request on stdin. The
helper picks the credential facet whose `hosts` include the request's host,
fetches that provider's credential from the broker route, and prints `username`
and `password`. `store` and `erase` do nothing, because the token is never
persisted.

In a managed sandbox, `omnigent host` calls `configure_host_credentials` at
startup and `start_credential_refresh` after it. Both do nothing elsewhere.
`configure_host_credentials` probes the broker once for each credential facet,
installs the helper on the hosts that qualify, makes a connected owner whose id
is an email address the commit author, and has the facet write its CLI config.
`configure_clone_credentials` installs the helper the same way before the
initial clone, and never removes an entry. `start_credential_refresh` starts one
daemon thread for each facet. Every `refresh_interval_s()` seconds the thread
re-fetches the credential and re-writes the CLI config, because a CLI such as gh
reads a static config and the provider token expires.

The `CredentialFacet` protocol in `omnigent/git_credential/__init__.py` is the
contract:

| Member | What it does |
| --- | --- |
| `hosts(instances)` | The lower-cased git hosts that the facet can serve over https. GitHub returns `github.com` plus the hosts of `OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS`. |
| `git_username(cred)` | The git username that goes with the broker's token. GitHub returns the broker's `username`, else `x-access-token`. |
| `write_cli_config(cred, home)` | Writes the credential into the provider CLI's config under `home`. It returns `True` when it wrote and `False` when it skipped or hit a filesystem error. GitHub merges `github.com` into gh's `hosts.yml`, keeps the other hosts, and skips a response whose `hosts` lack `github.com`. |
| `refresh_interval_s()` | Seconds between CLI config refreshes. Zero or less disables them. GitHub reads `OMNIGENT_GH_REFRESH_INTERVAL_S` and defaults to 1800. |
| `api_hosts()` | The lower-cased API hosts that the credential is valid for. GitHub returns `api.github.com`. Nothing calls it today. |

The helper vends a token for a host only when the host is in both the facet's
`hosts` and the `hosts` list of the broker response. A connected response with
no `hosts` list, which an older server sends, is definitive for the provider's
default hosts only, so a github.com token never reaches a configured GitHub
Enterprise host. The host installs the helper on the hosts that qualify. After a
definitive answer it also removes the helper's own entries from the facet's other
hosts, so an owner who has not linked the provider falls back to the ambient
helper. A helper that the user configured is never changed.

An inconclusive probe (a timeout, a network error, a 5xx, or a body that does
not parse) installs the helper on the provider's default hosts, so a broker
outage fails git authentication instead of falling back to the image's shared
token. It changes nothing on other hosts. The refresh thread applies the next
definitive answer.

`api_hosts()` exists for a later move of provider credentials to the egress
credential proxy. That proxy ([design](../designs/SANDBOX_CREDENTIAL_PROXY.md))
injects a credential into requests to the hosts it is bound to, so it needs the
hosts that a credential is valid for.

`omnigent/git_credential_github.py` is the earlier GitHub-only helper. Its names
are deprecated and will be removed in 0.19.0. They are `main`,
`configure_host_git`, `configure_host_gh`, `start_host_gh_refresh`,
`configure_clone_credentials`, and the private hooks beside them. Each one
delegates to `omnigent.git_credential` and keeps its signature and its
GitHub-only behavior.

They stay because the sandbox init container still runs this module.
`_render_workspace_prep_command` in `omnigent/onboarding/sandboxes/kubernetes.py`
renders a script that imports `main` and `configure_clone_credentials` from it
and replaces its private `_install_broker_helper`, and a sandbox image can run a
different version of Omnigent than the server that rendered the script. Before
0.19.0 the renderer has to switch to `omnigent.git_credential`, and sandbox
images have to include that package. The initial clone in a managed sandbox wires
only github.com, so a provider that needs clone support there has to extend that
script.

## The policy facet

The policy facet names the policy that gates a provider's commands and tool
calls. Its module exposes `POLICY`, the registry handler path of the provider's
policy. For GitHub the facet module is `omnigent/policies/builtins/github.py`,
and `POLICY` is `omnigent.policies.builtins.github.github_policy`, the `handler`
of the module's `POLICY_REGISTRY` entry.

A provider that declares a policy facet must register its policy with the policy
engine. The engine scans the `POLICY_REGISTRY` lists of the modules in
`BUILTIN_POLICY_MODULES` (`omnigent/policies/builtins/__init__.py`) and of the
modules that the server config lists in `policy_modules`, and `POLICY` has to be
the `handler` of an entry in one of them.

The GitHub policy keeps gating `git` commands for every remote, whichever
provider the remote belongs to. A provider's own policy adds its own decisions
for that provider's commands.

## Providers in `/v1/info`

`GET /v1/info` lists every registered provider in `git_providers`, in
registration order. `ServerInfoResponse` in `omnigent/server/app.py` declares the
field. The entry for GitHub on a server that has enabled its connection looks
like this:

```json
{
  "id": "github",
  "display_name": "GitHub",
  "capabilities": {
    "pull_requests": true,
    "connection": true,
    "repo_browser": true,
    "credential_broker": true
  }
}
```

| Capability | True when |
| --- | --- |
| `pull_requests` | The descriptor names a `pull_requests` facet. |
| `connection` | The server has enabled the provider's connection: its config and its store are both present, so its id is in `enabled_connections`. |
| `repo_browser` | `connection` is true and the connection facet's `repo_browser` is true. |
| `credential_broker` | `connection` is true and the descriptor names a `credential` facet. |

`git_provider_infos` reads the descriptors and the `repo_browser` flags that
`create_app` passes in, so it imports no facet module. `enabled_connections` is
unchanged: it still lists the id of every enabled connection, Databricks
included.

## The repo browser

The new-chat dialog lets a user pick a repository from a connected account. It
needs two routes from a provider whose connection facet has `repo_browser` set.
GitHub's router in `omnigent/server/routes/connections_github.py` serves them.

- `GET /v1/connections/{id}/repos` returns
  `{"connected": ..., "repos": [...], "truncated": ...}`. `connected` is false,
  with an empty `repos`, when the user has not linked the provider, so the dialog
  falls back to a pasted URL. Each repo has `full_name`, `clone_url`,
  `default_branch`, `private`, and `pushed_at`, newest push first. `truncated` is
  true when the page cap was hit and more repos exist.
- `GET /v1/connections/{id}/repos/{full_name}/branches` returns
  `{"connected": ..., "branches": [...]}`. `full_name` is the repo's
  provider-scoped name. For GitHub it is `owner/repo`, so the route has two path
  segments, and a name outside GitHub's character set gets HTTP 400.

Both routes return HTTP 502 when the provider API fails.

`web/src/lib/connectionsApi.ts` calls the routes with
`fetchConnectionRepos(providerId)` and
`fetchConnectionBranches(providerId, fullName)`, and its `ConnectionRepo`,
`ConnectionRepoList`, and `ConnectionBranchList` types describe the responses.
`fetchGithubRepos`, `fetchGithubBranches`, and the `GithubRepo`,
`GithubRepoList`, and `GithubBranchList` types in
`web/src/lib/githubIntegration.ts` are deprecated wrappers and will be removed in
0.19.0.

`NewChatLandingScreen` in `web/src/shell/NewChatDialog.tsx` shows one picker for
each provider in `gitProviders(info)` whose `connection` and `repo_browser`
capabilities are both true. `gitProviders(info)` in
`web/src/lib/capabilities.ts` returns `info.git_providers`. A server that
predates the field has GitHub's connection exactly when `enabled_connections`
names `github`, so in that case `gitProviders` returns one GitHub entry with
every capability true, and otherwise an empty list. The parser that reads
`git_providers` drops entries that lack an id, a display name, or a capabilities
object, and repeated ids, and it counts a capability only when it is `true`.

A repo's clone URL is its `clone_url`, else `cloneUrlFor(full_name)` from the
provider's `GIT_PROVIDERS` entry. A repo that has neither is left out of the
picker. The picker labels itself with the provider's `label` from
`gitProviderCopy`. TanStack Query caches the lists under
`["connection-repos", providerId]` and the branches under
`["connection-branches", providerId, fullName]`, both with a five-minute
`staleTime`.

## Stable wire identifiers

Other processes, other versions of this code, saved browser state, and files on
disk refer to the identifiers below by name. Renaming one breaks them. The pull
request identifiers keep the `github` name for every provider, including Azure
DevOps.

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
| `/v1/connections/{id}/*` routes: `connect`, `callback`, `status`, `disconnect`, and for a provider with a repo browser `repos` and `repos/{full_name}/branches` | `omnigent/server/routes/connections_base.py`, `omnigent/server/routes/connections_github.py`, `web/src/lib/connectionsApi.ts`, `web/src/lib/githubIntegration.ts` |
| `/v1/hosts/{id}/credentials/{provider}` | `omnigent/server/routes/host_credentials.py`, `omnigent/git_credential/__init__.py` |
| The 404 body `{"detail": "unknown credential provider"}` of that route | `omnigent/server/routes/host_credentials.py`. The helper treats only this 404 as "the server does not broker this provider", and any other 404 as inconclusive (`_UNKNOWN_PROVIDER_DETAIL` in `omnigent/git_credential/__init__.py`). |
| The sandbox helper source `import os,sys; from omnigent.git_credential_github import main; sys.exit(main(['--server',<server url>,'--host-id',<host id>,'--host-token',os.environ['OMNIGENT_HOST_TOKEN'],*sys.argv[1:]]))` | `_render_workspace_prep_command` in `omnigent/onboarding/sandboxes/kubernetes.py`. git runs it as `!python3 -Ic '<source>'` for `credential.https://github.com.helper`. |

## Adding a provider

Steps 1 to 7 have a counterpart in the Azure DevOps provider, and steps 8 to 11
have one in the GitHub provider.

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
8. Write the connection facet if users link an account with the provider. Add
   `omnigent/server/git_providers/<id>.py` exposing `CONNECTION`, a
   `ConnectionFacet`, and name it in `facets.connection`. Import server modules
   inside the methods, as `omnigent/server/git_providers/github.py` does, so
   loading the facet stays cheap. A provider that connects with OAuth can build
   its router on `create_connection_router` and `ConnectionHooks` in
   `omnigent/server/routes/connections_base.py`, as
   `omnigent/server/routes/connections_github.py` does. Let `config_from_env`
   return `None` when the environment does not configure the provider, and raise
   when it configures it wrongly. `tests/server/test_connections_registry.py`
   registers a fake provider at run time and drives `create_app` with it.
9. Write the credential facet if sandboxes need the provider's token. Add
   `omnigent/git_credential/<id>.py` exposing `CREDENTIAL`, a `CredentialFacet`,
   and name it in `facets.credential`. Return the provider's hosts from `hosts`,
   and have the connection facet's `resolve_credential` return the same hosts in
   its `hosts` list, so the helper vends only for hosts that both sides name.
   Test the facet in `tests/test_git_credential.py` and the broker route in
   `tests/server/routes/test_host_credentials.py`. The initial clone in a managed
   sandbox wires only github.com, so also extend the init container script in
   `omnigent/onboarding/sandboxes/kubernetes.py` if the provider needs clone
   support there.
10. Add the repo browser if the provider can list repositories. Set
    `repo_browser = True` on the connection facet, serve the two repo routes with
    the responses described above, and add `cloneUrlFor` to the `GIT_PROVIDERS`
    entry unless the repo list carries `clone_url`. Test the client in
    `web/src/lib/connectionsApi.test.ts` and the picker in
    `web/src/shell/NewChatDialog.test.tsx`.
11. Write the policy facet if the provider has its own policy. Register the
    policy in a `POLICY_REGISTRY` that the engine scans, expose its handler path
    as `POLICY` in the module that `facets.policy` names, and test that `POLICY`
    is a registered handler.

Run the provider layer's Python and web tests with:

```bash
uv run --no-sync pytest tests/git_providers tests/runner/test_git_provider_protocol.py tests/runner/test_pr_resource.py tests/server/test_connections_registry.py tests/test_git_credential.py tests/server/routes/test_host_credentials.py
pnpm --dir web exec vitest run src/lib/gitProviders.test.ts src/lib/capabilities.test.ts src/lib/connectionsApi.test.ts
```
