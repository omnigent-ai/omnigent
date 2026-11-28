# Verify a branch on its own dev server

Test a change end to end on a throwaway server built from your own worktree,
instead of restarting a shared server. Each instance has its own free port and
a fresh run dir under `/tmp` for its database, artifacts, config and logs.
Several instances can run side by side.

## 1. Start the server

Always pass your own worktree's path to `up`, or run it from inside your own
worktree. An instance belongs to the worktree it was started from, and that
worktree's `down --all` stops it.

```bash
dev/branch-server.sh up                # this worktree
dev/branch-server.sh up ~/path/to/your-worktree  # by path
dev/branch-server.sh up --with-host    # also start a host, to run agent sessions
```

`up` symlinks `node_modules` and `web/node_modules` from the main checkout if
they're missing. It rebuilds the web UI when any file under `web/` is newer
than the last build (use `--rebuild` to force a build or `--no-build` to skip
it). It also runs the worktree's own Python code through the main checkout's
`.venv`. When it's done it prints:

```text
RUN_DIR=/tmp/omnigent-branch-<branch>-XXXXXX
PORT=49229
SERVER_PID=...
HOST_PID=...      # empty without --with-host
URL=http://127.0.0.1:49229
```

The server starts in local single-user mode (owner `local`), so it needs no
login. It never reads or writes `~/.omnigent`, and it refuses the shared ports
6767 and 6768. Before launching, it clears any `OMNIGENT_*` variables inherited
from an agent session, so the new processes don't log into or act as part of
that session. Logs are in `$RUN_DIR/logs/` and `$RUN_DIR/data/logs/`.

To test a change behind a release feature or a setting, opt in by name. Only
the variables you name reach the server and host; everything else stays
cleared:

```bash
dev/branch-server.sh up --features memory          # sets OMNIGENT_FEATURES
dev/branch-server.sh up --env OMNIGENT_SLACK_AUTOMATION_API_URL=http://127.0.0.1:9000
dev/branch-server.sh up --env OMNIGENT_SLACK_BOT_TOKEN  # pass your current value
```

`--env` is repeatable and takes `OMNIGENT_*` names only. A bare name passes
the caller's current value, which keeps secrets out of the command line. It
refuses the variables the launcher sets itself (data dir, config, database,
web UI, features) and `OMNIGENT_AUTH_*` (use `--auth-header`). It also refuses
any value that points at `~/.omnigent`, a `chat.db` or the shared ports. `up`
prints the opted-in names (never values) as `FEATURES=` and `ENV=`.

Python dependencies come from the main checkout's venv. If your branch adds or
changes a dependency, install it there first.

## 2. Seed data through the API

The database starts empty. Create what the test needs over HTTP:

```bash
curl -s -X POST "$URL/v1/projects" -H 'content-type: application/json' \
  -d '{"name":"zz-e2e-demo"}'
```

## 3. Drive the UI and capture evidence

If the desktop browser pane is connected, drive `$URL` with the browser tools
(`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_screenshot`).

Otherwise, use the headless helper. It clicks targets in order (by button or
link name, then by visible text), waits for each expected text, and saves a
full-page screenshot. It grants clipboard permissions and uses any Chromium in
the Playwright cache. When a step fails it exits non-zero and also saves
`<out>-failure.png`.

```bash
.venv/bin/python -I dev/branch_screenshot.py "$URL/projects/zz-e2e-demo" /tmp/evidence/copy-url.png \
  --click "Project settings" --click "Copy URL" --expect "Copied"
```

Run it with the main checkout's `.venv/bin/python` when you're in a worktree.
Save the screenshot to the ticket as a project artifact
(`sys_project_artifact_save` with the ticket's `ticket_id`). Don't post a local
path as the evidence.

## Owner and member checks (`--auth-header`)

By default the server runs in single-user mode, so every request is the same
user. To check what an owner may do that a member may not, start it with
header sign-in. Each request then acts as the user named in its
`X-Forwarded-Email` header, and a request without one gets 401:

```bash
eval "$(dev/branch-server.sh up --auth-header | grep -E '^(URL|RUN_DIR)=')"
OWNER=(-H 'X-Forwarded-Email: owner@example.com')
MEMBER=(-H 'X-Forwarded-Email: member@example.com')
JSON=(-H 'content-type: application/json')

# Project agents must exist, so look two up.
agent() { curl -s "${OWNER[@]}" "$URL/v1/agents" | jq -r --arg n "$1" '.data[] | select(.name==$n) | .id'; }
CLAUDE=$(agent claude-native-ui) CODEX=$(agent codex-native-ui)

# The owner makes a project and shares it with the member.
PROJECT=$(curl -s "${OWNER[@]}" "${JSON[@]}" -X POST "$URL/v1/projects" \
  -d "{\"name\":\"zz-e2e-members\",\"config\":{\"agent_id\":\"$CLAUDE\"}}" | jq -r .id)
curl -s "${OWNER[@]}" "${JSON[@]}" -X POST "$URL/v1/projects/$PROJECT/shares" \
  -d '{"user_id":"member@example.com"}' > /dev/null

# Owner-only setting (the project's agent): 403 for the member, 200 for the owner.
curl -s -o /dev/null -w '%{http_code}\n' "${MEMBER[@]}" "${JSON[@]}" -X PATCH \
  "$URL/v1/projects/$PROJECT" -d "{\"config\":{\"agent_id\":\"$CODEX\"}}"   # 403
curl -s -o /dev/null -w '%{http_code}\n' "${OWNER[@]}" "${JSON[@]}" -X PATCH \
  "$URL/v1/projects/$PROJECT" -d "{\"config\":{\"agent_id\":\"$CODEX\"}}"   # 200
```

In the browser, act as a user by sending the same header with every request.
The screenshot helper takes `--header` (repeatable) and can record a video:

```bash
.venv/bin/python -I dev/branch_screenshot.py "$URL/projects/zz-e2e-members" /tmp/evidence/member.png \
  --header "X-Forwarded-Email:member@example.com" --video /tmp/evidence/video \
  --click "Edit team" --expect "Only the project owner"
```

In your own Playwright script, pass
`extra_http_headers={"X-Forwarded-Email": "member@example.com"}` to
`browser.new_context(...)`. Use one context per user to switch between owner and
member in the same run. `--auth-header` can't be combined with `--with-host`,
because a host can't sign in by header.

If a page sticks on "Loading Omnigent…" with 404s for `/assets/...`, the web
build it serves was rebuilt after the server started. Restart the server.

## 4. Tear down

```bash
dev/branch-server.sh down "$RUN_DIR"   # stop it and delete the run dir
dev/branch-server.sh down "$RUN_DIR" --keep   # stop it, keep the logs
dev/branch-server.sh down --all        # every instance from the current worktree
dev/branch-server.sh list              # state, URL, run dir and worktree of each
```
