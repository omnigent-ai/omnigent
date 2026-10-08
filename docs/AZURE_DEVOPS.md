# Azure DevOps pull requests

The session pull request panel works with repositories on Azure DevOps Services
alongside GitHub and GitLab. The workspace's git remote selects the provider. This page
covers what the host needs and what the first version does not do.

For descriptor discovery, see [Git providers](git-providers.md). The runtime
contract is described in [Pull request providers](git-pull-requests.md).

## Sign in on the host

The panel calls the Azure DevOps REST API with the host's own credential. It
uses the first one it finds:

1. `AZURE_DEVOPS_EXT_PAT`, a personal access token with **Code (Read)** scope.
   It is the variable that the `az devops` extension also reads.
2. An `az login` session. The host runs `az account get-access-token` for the
   Azure DevOps resource and reuses the token until five minutes before it
   expires. If `az` returns a token close to expiry, it is reused for up to one
   minute, capped at its actual expiry. Expired tokens are rejected. When no
   one is signed in, it tries again after a minute.

Before either of these, `resolve_token` reads
`~/.config/omnigent/azure-devops/token.json` (`access_token` and `expires_at`,
in epoch seconds) and uses its token until `expires_at` passes. Nothing writes
this file yet, and it is reserved for a later sandbox credential part.

The host looks for `az` on `PATH`, then at `/opt/homebrew/bin/az` and
`/usr/local/bin/az`. Processes started by a service manager or a desktop app
often run without Homebrew on `PATH`.

Requests and the `az` call time out after 15 seconds. Set
`OMNIGENT_AZURE_DEVOPS_TIMEOUT_SECONDS` to change that. Panel requests use an
eight-second budget when starting REST calls and waiting for background fetches,
and show the data loaded within that budget. Individual network phases and local
Git commands can outlast it, so slow requests may reach the runner proxy's timeout.

The panel labels Azure requests with `!`, such as `!42`, using the provider's
display metadata. Failed lookups show a warning. Partially loaded checks and
comments remain visible with an incomplete marker; the first 100 comments are
shown with a `+` count when more exist. Failed or capped file-list requests also
retain loaded files and identify the incomplete result. A missing diff shows a
reason instead of implying there are no changes.

## Remotes and pull request URLs

The panel reads each remote's configured URL, before any
`url.<base>.insteadOf` rewrite, and uses the first Azure DevOps remote, with
`origin` first. It accepts these forms, with or without a `.git` suffix:

- `https://dev.azure.com/{org}/{project}/_git/{repo}`, including the
  `https://{org}@dev.azure.com/...` form from the clone dialog
- `https://dev.azure.com/{org}/_git/{repo}`, a project's default repository
- `https://{org}.visualstudio.com/[DefaultCollection/]{project}/_git/{repo}`
- `git@ssh.dev.azure.com:v3/{org}/{project}/{repo}` and
  `ssh://git@ssh.dev.azure.com/v3/{org}/{project}/{repo}`
- the same SSH forms on `vs-ssh.visualstudio.com`

To link a pull request by hand, paste its web URL:
`https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{id}`, or the
same path on a `visualstudio.com` host. The query string and fragment are
dropped. The session stores the URL on `dev.azure.com` with the organization,
project, and repository in lower case, because Azure DevOps names are not case
sensitive.

## Only dev.azure.com is contacted

Corporate TLS proxies often intercept `vssps.dev.azure.com`, the Azure DevOps
identity service, and certificate checks against it then fail. The client
therefore sends requests only to `https://dev.azure.com` and refuses any other
host. It never looks up a user by display name or email address; it uses the
ids that come back with pull requests and comments. It never runs `az devops`
or `az repos`.

## Limits of the first version

- **No sandbox credentials.** A managed sandbox has no Azure DevOps credential,
  so the panel works only on a host where you signed in yourself.
- **No account switching.** The host's one credential reads every pull
  request, and the panel shows no account or remote selector.
- **No diff for a pull request outside the workspace's remote.** The whole-PR
  diff comes from local git. The panel shows it only when the pull request's
  repository is one of the workspace's remotes. When the commits are missing,
  it fetches the target branch, the source branch, and then
  `refs/pull/<id>/merge` from that remote, each only while a commit is still
  missing, without credential prompts. The fetch runs in the background for up
  to two minutes, so a fetch that outlasts one request lands for a later one.
  After a fetch fails, the panel does not fetch that pull request again for a
  minute.
- **No line counts.** Azure DevOps does not return added and removed line
  counts for changed files.
- **Slow local diffs.** Local Git commands have separate 30-second timeouts.
  Large or slow checkouts can exceed the runner proxy's request limit even
  when REST calls and background-fetch waits stay within their budget.
- **Azure DevOps Services only.** Azure DevOps Server (on premises) is not
  supported.

## Observer limits

The session also records the pull requests that its agent creates or changes.
For Azure DevOps, it finds them in two places:

- the JSON that `az repos pr create` and `az repos pr update` print
- pull request web URLs printed in tool output

`az repos pr set-vote` and the `reviewer`, `work-item`, and `policy` commands
print no pull request, so they do not record one.

When one shell command runs an `az repos pr` write together with a PR read such
as `show` or `list`, the observer cannot tell which output belongs to the write,
and because a write rarely names its PR in its arguments, it usually records no
PR.

## Verify the integration

On a host with an Azure repository, run `az login` or set
`AZURE_DEVOPS_EXT_PAT` before starting Omnigent. Open a session in that repository,
then click the composer’s `!N` link or open **Pull Requests** in the workspace
panel. Confirm the Azure DevOps heading, request title, Summary, and Changes.
Repeat at a mobile viewport to check the full-screen drawer. Link a request from
another repository and confirm its summary loads while the diff explains the
workspace limitation.

The automated suites under `tests/runner/test_azure_devops_*` and
`tests/e2e_ui/azure_devops/` use local Git and mocked REST responses. They verify
timeouts, incomplete results, background fetches, and desktop/mobile rendering
without a live Azure organization.
