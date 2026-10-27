# Remote agent launch

Users run a registered agent with `omnigent run --server <url>`, choose from
the server and their own registrations, and send a prompt interactively or
with `-p`. New sessions use the selected registration; existing sessions keep
their own agents. Without a matching online runner, a new session can launch
one on this machine's connected host.

## Sub-features

- `catalog`: fresh and used servers offer the same registered agents; unused
  registrations remain visible and session-only uploads are not candidates.
- `selection`: one candidate is automatic; several offer a numbered/name menu
  with source and harness. `--agent <name>` selects explicitly; ambiguous
  headless runs fail without creating a session.
- `identity`: the selected registration survives same-name uploads and startup;
  server registrations take precedence over same-named user registrations.
- `runner-lifecycle`: adopt a matching online runner or launch on this machine's
  suitable connected host. One-shots stop only the runner they launched;
  interactive host-launched runners remain host-managed. Launch progress clears
  when the request finishes or fails; one-shot progress stays on stderr.
- `existing-session`: resume uses the existing session's agent; fork copies the
  source without a catalog picker. Headless fork is rejected.
- `errors`: empty catalogs, unknown names, unreachable servers, authentication,
  access denial, malformed responses, and incomplete pagination fail clearly.

## How to get to it (user POV)

- Interactive: `omnigent run --server <url>`; choose a registered agent and type
  a prompt. Repeat after a conversation exists to check unused registrations.
- Explicit selection: `omnigent run --server <url> --agent <name>`.
- One-shot: add `-p "hi"`; with several registrations, omit `--agent` once to
  check the selection error rather than an interactive prompt.
- Connected-host launch: start `omnigent host --server <url>` on this machine,
  then use either new-session entry point with no matching runner online.
- Existing session: `omnigent run --server <url> --resume <session-id>` or
  `omnigent run --server <url> --fork <session-id>`.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). The tests
below use their own isolated services or mocked transport; do not start the
default verification environment for them. Its prestarted runner would mask
the host-launch case. Use this checkout's frozen dependencies, a local mock
model, isolated homes, and only fixture-owned processes. Interactive process
coverage requires `pexpect`; a skipped variant is not proof.

1. Register A and B on an isolated server. Run A with `--agent A -p "hi"`;
   then choose unused B in the interactive menu and verify its reply.
2. With both agents registered, omit `--agent` from a headless run. Verify the
   error lists choices and creates no session.
3. Connect this machine's host with no matching runner online. Exercise both
   interactive and one-shot launches; verify the selected registration, host,
   workspace, reply and runner ownership after exit. Confirm the waiting message
   appears during launch and clears before the reply in a terminal. Redirected
   one-shot stderr keeps the progress line; stdout contains only the reply.
4. Resume or fork an existing session and verify its original agent is used.
   Within the REPL, `/new` keeps that agent's ID and harness, including unnamed
   uploads, without inheriting a previous launch's routing header. If no runner
   or harness is known, it asks you to start with `--agent` rather than guessing.

The process scenarios live in
`tests/e2e/test_remote_url_chat_fresh_session_2437.py`; catalog, host policy,
error and routing matrices live in the component suites below.

Run component coverage without browser flags:

```sh
uv run --no-sync pytest -q tests/cli/test_chat.py tests/cli/test_cli.py \
  tests/cli/test_remote_launch.py tests/cli/test_cli_diagnostics.py \
  tests/cli/test_login_databricks.py tests/repl/test_repl_remote_agent_lookup.py \
  tests/cli/test_session_host.py tests/cli/test_cli_auth.py \
  tests/frontends/sdk/test_agent_catalog.py \
  tests/frontends/sdk/test_sessions_namespace.py --reruns 0
```

Run the own-environment process scenarios with the default mock model:

```sh
uv run --no-sync pytest -q \
  tests/e2e/test_remote_url_chat_fresh_session_2437.py \
  --reruns 0
```

For terminal evidence, record the command, transcript or timed terminal video,
exit status, tested revision, and read-only session/runner checks. Keep media
outside the tracked tree; upload it as a PR attachment. A video of one entry
point does not establish the others.

## Gotchas

- Local YAML upload and native harness launch are separate workflows, not
  additional entries in the remote catalog picker. See
  [Custom agents](custom-agents.md) for Settings and
  [Login and host authentication](login-and-host-auth.md) for real sign-in.
- This machine's host is required because the workspace is the CLI's local cwd.
  Do not select another machine or stop a developer's existing runner to test.
- Resume and fork do not gain automatic offline-runner relaunch. Interactive
  resume of an old snapshot with no agent name can still fall back to a menu;
  headless resume without a usable harness must fail rather than guess.
- A create-time timeout before the session ID arrives leaves the launch
  outcome unknown. A late runner may remain running; check the host before
  retrying. The CLI cannot stop a session whose ID it never received.
- Catalog IDs identify registrations, not immutable spec revisions. Optional
  user-listing compatibility errors differ from a failed primary catalog.
- Local mock-model process tests do not prove live provider authentication,
  OS-sandbox startup, or compatibility with pinned older server/runner builds.
  Record those separately when actually exercised; map consistency checks do
  not run the user journeys.
