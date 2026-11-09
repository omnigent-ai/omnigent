# Forgejo pull requests

Forgejo, Gitea, and Codeberg sessions show their pull requests in the shared
Pull Requests panel, composer controls, and Canvas cards. The runner reads the
`/api/v1` REST API with the execution host's fj or tea login, or anonymously
for public repositories. Agent fj, tea, MCP, and AGit writes associate the pull
request with the session.

## Sub-features

- `summary`: title, description, state, draft, labels, and commit status checks.
- `comments`: issue comments and review summaries, oldest first.
- `changes`: changed files with line counts, the unified diff, and file context
  pinned to the merge base and head.
- `associations`: link, select, and unlink a pull request from another repository.
- `partial`: incomplete comments, checks, or files keep loaded data and warn.
- `auth`: a denied or private read shows the fj or tea sign-in hint; Codeberg
  public repositories need no login.
- `discovery`: Codeberg, `OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS`, and fj or tea
  login hosts are recognized without a restart.
- `tracking`: successful writes persist; reads, comments, and failures do not.

## How to get to it (user POV)

- `desktop-rail`: open a session's Workspace sidepanel and choose Pull Requests.
- `desktop-composer`: click the pull request number beside the session composer.
- `mobile-composer`: tap the pull request number to open its full-screen panel.
- `canvas`: enable Canvas, open it, and follow a pull request link on a session card.
- `agent-tool`: create or change a pull request through the session's agent shell or MCP tools.

## Driving it with the repro environment

Preconditions: use the [Verify Omnigent skill](skills/verify-omnigent/SKILL.md)
and an isolated instance. No browser fixture covers Forgejo yet; the panel
reuses the shared provider UI, so drive it against a real instance. A local
Forgejo container works: run `codeberg.org/forgejo/forgejo:13` with HTTPS, create
a user with `forgejo admin user create`, and add its token with
`fj -H localhost:<port> auth add-token` in a throwaway `XDG_DATA_HOME`.

- `desktop-rail`, `desktop-composer`, and `mobile-composer`: start a session in a
  checkout whose branch has an open pull request. The panel shows its `#` number,
  checks, comments, and files; expanding a file shows merge-base and head content.
  Payload rules are covered by
  `tests/runner/test_forgejo_provider.py::test_workspace_branch_pull_request_fills_the_shared_payload`.
- Associations: paste a pull request URL from another repository, select it, and
  remove it. A Codeberg public URL loads without a login.
- `auth`: make the repository private and remove the login. The panel shows the
  sign-in hint, as in
  `tests/runner/test_forgejo_provider.py::test_codeberg_reads_are_anonymous_and_a_denial_shows_the_sign_in_hint`.
- `canvas`: with Canvas enabled, follow the card's link and confirm it opens
  `https://<host>/<owner>/<repo>/pulls/<n>`.
- `agent-tool`: in an agent session, run `fj -H <host> pr create -r owner/repo`,
  `tea pulls create`, and an AGit push to `refs/for/main`. Each pull request
  appears as an association. Attribution rules are covered by
  `tests/runner/test_forgejo_observer.py::test_agit_push_tracks_the_reported_pull_request`
  and the other tests in that file.

## Gotchas

- Gitea instances are labeled Forgejo; both use this one provider.
- A token goes only to the exact `host[:port]` of its fj or tea login. Hosts from
  the environment list or the Codeberg default are read anonymously, so a
  private repository on them shows the sign-in hint.
- `fj pr create` prints only `#N`, so tracking needs the host and repository in
  argv. Quote `'owner/repo#N'` arguments; the observer's shell lexer reads an
  unquoted `#` as a comment.
- AGit pull requests have no head branch, so branch inference does not find
  them; tracking or a pasted URL does.
- The local container needs a CA-signed leaf certificate. fj uses rustls, which
  rejects a self-signed CA certificate used as the server certificate.
- Keep screenshots and recordings outside the tracked tree.
