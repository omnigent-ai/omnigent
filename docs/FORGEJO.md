# Forgejo pull requests

The session pull request panel shows pull requests from Forgejo, Gitea, and
Codeberg. They share the `/api/v1` REST API, so one `forgejo` provider covers
all three and labels them "Forgejo". The panel reads the API directly over
HTTPS; it runs no CLI.

## Hosts and tokens

Codeberg works without setup. Public repositories are read anonymously.

For a self-hosted instance or a private repository, sign in with forgejo-cli
(`fj`) or tea on the execution host:

```sh
fj -H git.example.com auth login        # or: fj -H git.example.com auth add-token
tea login add --url https://git.example.com --token <token>
```

Omnigent reads the saved logins and needs no Omnigent setting or restart:

| CLI | File |
| --- | --- |
| fj (Linux) | `$XDG_DATA_HOME/forgejo-cli/keys.json`, default `~/.local/share/forgejo-cli/keys.json` |
| fj (macOS) | `~/Library/Application Support/forgejo-cli.forgejo-cli/keys.json`, then the legacy `Cyborus.forgejo-cli` directory |
| fj (Windows) | `%APPDATA%\forgejo-cli\forgejo-cli\data\keys.json`, then the legacy `Cyborus` directory |
| tea | `$XDG_CONFIG_HOME/tea/config.yml`, else the platform config directory (`~/.config`, `~/Library/Application Support`, `%LOCALAPPDATA%`), then `~/.tea/tea.yml` |

Each login is keyed by its exact HTTPS authority, `host[:port]`. A token goes
only to the authority of the login it came from. Every other recognized host is
read anonymously:

- Codeberg, unless a login for `codeberg.org` exists.
- Hosts added with `OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS`, a comma-separated
  list such as `git.example.com,git.example.com:8443`. They never receive a token.
- A host whose login names a different port.

When fj and tea both have a login for one authority, fj's token is used. tea
OAuth logins keep their current token in tea's credential store, so Omnigent
reads those hosts anonymously. fj logins whose key includes a path (an instance
served under a subpath) are not supported. HTTP logins are ignored.

An SSH remote maps to the one recognized HTTPS authority with the same
hostname. When two authorities share a hostname, use an HTTPS remote. Remote and
pull request URLs with embedded credentials are rejected.

## Panel and tracking

Open the Pull Requests tab in the session's Workspace sidepanel, or click the
number beside the composer. The current branch's open pull request is found in
the branch's upstream remote, then `origin`, the other Forgejo remotes, and the
source repository's fork parent. The pull request's head must be the current
branch of the source repository: `branch.<name>.pushRemote`, then
`remote.pushDefault`, then the first remote. AGit pull requests have no head
branch, so attach them by URL or let tracking add them.

The panel shows the title, description, state (open, merged, or closed), draft
status, labels, comments and review summaries, commit status checks, changed
files with line counts, and the unified diff. Expanded file context reads the
merge base and the head revision. Check states map as follows: `success` and
`warning` pass, `failure` and `error` fail, `pending` is pending.

Successful agent operations add their pull request to the session:

- `fj pr create` when the command names the host and repository, as in
  `fj -H git.example.com pr create -r owner/repo` or
  `fj pr create -r git.example.com/owner/repo`. fj prints only the number.
- `fj pr merge|close|edit|assign|unassign` with a full `'[host/]owner/repo#N'`
  argument and the host. Quote the argument: the observer reads `#` as the start
  of a shell comment.
- `tea pulls create|edit|approve|reject`, from the URL tea prints or its
  `--output json`. `tea pulls merge|close|reopen` print nothing and name the
  pull request only by number, so they are not tracked.
- gitea-mcp `pull_request_write` and forgejo-mcp `create_pull_review`,
  `submit_pull_review`, `create_review_requests`, and `delete_review_requests`,
  when the result names the requested pull request on a recognized host.
  forgejo-mcp's `create_pull_request`, `update_pull_request`, and
  `merge_pull_request` share GitHub's tool names; the result's Forgejo URL
  still identifies them.
- AGit pushes such as `git push origin HEAD:refs/for/main -o topic=x`, from the
  pull request URL Forgejo prints. These count as worked on, since the same push
  updates an existing pull request.

Reads, comments, comment-only reviews, `--help`, `--web`, dry runs, and failed
commands are not tracked. Tracking makes no network requests.

## Limits and deferred work

Each panel request has an eight-second budget. Paginated lists stop at ten pages
of 50 items and are marked incomplete. Login and remote selection stay in fj and tea;
the panel has no account or base remote switcher.

These are separate work: sandbox git credentials for a second host, an OAuth
connection in Omnigent, the new-chat repository picker, write actions in the
panel (merge, labels, reviewers, comments, editing, attachments), and the git
policy's handling of Forgejo URLs.

To verify manually:

1. Sign in with `fj -H <host> auth login` on the execution host. Start a session
   in a checkout whose branch has an open pull request. Open the Pull Requests
   tab and compare the state, description, comments, checks, and files with the
   Forgejo web page.
2. Open a changed file and expand context. A renamed file shows its old content
   at the merge base.
3. Attach a Codeberg pull request URL from a public repository. It loads without
   a login.
4. In an agent session, run `fj -H <host> pr create -r owner/repo "Title" --body x`
   and an AGit push. Both pull requests appear as session associations.
