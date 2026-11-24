# Verify a branch on its own dev server

Test a change end to end on a throwaway server built from your own worktree,
instead of restarting a shared server. Each instance has its own free port and
a fresh run dir under `/tmp` for its database, artifacts, config and logs.
Several instances can run side by side.

## 1. Start the server

```bash
dev/branch-server.sh up                # this worktree
dev/branch-server.sh up ../other-tree  # another worktree
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

## 4. Tear down

```bash
dev/branch-server.sh down "$RUN_DIR"   # stop it and delete the run dir
dev/branch-server.sh down "$RUN_DIR" --keep   # stop it, keep the logs
dev/branch-server.sh down --all        # every instance from the current worktree
dev/branch-server.sh list              # state, URL, run dir and worktree of each
```
