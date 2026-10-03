# GitLab merge requests

The session pull request panel shows GitLab merge requests using `glab` on the
execution host. Sign in there before opening the panel:

```sh
glab auth login --hostname gitlab.com
glab auth status --hostname gitlab.com
```

Omnigent uses the CLI's existing credentials. It does not require a GitLab OAuth
connection in Omnigent. Install and authenticate `glab` inside a sandbox when
that sandbox runs the session.

## Private instances and remotes

Set the allowed GitLab HTTPS authorities in the execution host's environment
before starting its runner:

```sh
export OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS=git.example.com:8443
GITLAB_HOST=git.example.com:8443 glab auth login --api-host git.example.com:8443
```

The setting accepts comma-separated hostnames, hostnames with ports, or HTTPS
origins. `GITLAB_HOST` and `GLAB_HOST` also identify a trusted instance.
GitLab.com is supported by default. Nested groups and project-local MR numbers
are preserved:

```text
https://git.example.com:8443/company/team/project.git
git@git.example.com:company/team/project.git
https://git.example.com:8443/company/team/project/-/merge_requests/42
```

An SSH remote maps to the configured HTTPS authority for its hostname. Its SSH
port does not become the API port. When two allowed authorities share a hostname,
use an HTTPS remote to select the intended one.

## Panel and tracking

Open the Pull Requests tab in the session's Workspace sidepanel, or click the
MR number beside its composer. The current branch's merge request is inferred from its upstream
remote, then `origin`, then other GitLab remotes and the fork's parent project.
The source project comes from the configured push remote or `origin`; discovery
requires both its project ID and the current branch to match. Paste an MR URL into the panel
to attach another accessible merge request. Removing it prevents automatic
tracking from adding it again; attaching it manually restores it.

The panel shows the title, description, state, draft status, comments, pipeline
jobs and downstream jobs, changed files, and diff. Expanded text-file context
reads the target project at the MR's merge-base and the source project at its
head revision, including fork merge requests. Refresh after the MR changes
before expanding context again.

Successful `glab mr` mutations and `glab api` MR writes are tracked from their
explicit target or result. Supported GitLab MCP mutation tools are tracked too.
Read commands, comments, failed commands, and URLs quoted inside descriptions
do not create associations. For MCP fork creation, supply `target_project_id`
when the tool supports it; ambiguous source-project paths may require linking
the resulting MR manually. Tracking does not make API calls.

## Limits and verification

Each panel request has an eight-second CLI budget. Lists are limited to 500
items. The panel marks incomplete comments, checks, and file lists; omitted,
large, binary, or metadata-only patches direct you to GitLab. A failed content
read is an error rather than an apparent file deletion.

Account selection remains in `glab`; the panel has no GitLab account or base
remote switcher. Repository picking, webhook events, and managed sandbox
credential provisioning are separate features.

To verify manually:

1. Start a session in an authenticated GitLab checkout with an open MR and open
   its Pull Requests tab. Confirm the `!` number, description, comments, and pipeline
   results match GitLab.
2. Open a changed file, then expand context. For a fork MR, check a renamed file
   against the target merge-base and source head in GitLab.
3. Attach a second MR URL, select it, remove it, and refresh. Confirm it remains
   removed, then attach it again.
4. Repeat through the compact composer control and at mobile width. Without
   `glab` authentication, confirm the panel keeps the checkout context and shows
   an actionable sign-in or access hint.
