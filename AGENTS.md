# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## Committing

Run the `pre-commit` hook before committing (`pre-commit run --all-files`, or
let it run on staged files via `git commit`). Fix any issues it reports so the
commit lands clean — CI runs the same checks.

## Local development shortcuts

Use `just` for common tasks; run `just --list` for grouped recipes.

- `just ensure` — install/check prerequisites
- `just run-ios` / `just run-android` — build/run mobile apps
- `just dev` / `just dev-mobile` — start the omnigent dev pod
- `just electron-dev` / `just electron-build` — Electron desktop shell
- `just lint` / `just lint-all` — run pre-commit
- `just normalize-locks` — rewrite lockfile registries to PyPI/npmjs.org

## Runtime and database safety

This checkout is the live Omnigent installation. Preserve its runtime state,
host identity, database, and active Git branch unless the user explicitly asks
for a migration, reset, or replacement.

### Services

- The authoritative local services are the user-systemd units
  `omnigent-server.service` and `omnigent-host.service`.
- Their working tree is `/mnt/storage/omnigent-opencode-merge`. Keep the
  server, host, runners, and new sessions on this checkout and its current
  branch.
- Do not launch a manual `omnigent server` or `omnigent host` alongside the
  systemd units. Do not kill a working process just because it is not attached
  to the current terminal; first determine whether it belongs to systemd.
- Before changing runtime state, check the branch and service ownership:

  ```bash
  git branch --show-current
  systemctl --user is-active omnigent-server.service omnigent-host.service
  curl -fsS http://127.0.0.1:6767/health
  ```

- After changing a unit, run `systemctl --user daemon-reload`. Restart the
  server first, wait for `/health` to succeed, then restart the host. Verify
  both units are `active` and inspect their user journal before handing off:

  ```bash
  systemctl --user restart omnigent-server.service
  until curl -fsS http://127.0.0.1:6767/health >/dev/null; do sleep 1; done
  systemctl --user restart omnigent-host.service
  systemctl --user is-active omnigent-server.service omnigent-host.service
  journalctl --user -u omnigent-server.service -u omnigent-host.service -n 80 --no-pager
  ```

### Host identity and credentials

- Preserve the existing host identity in `~/.omnigent/config.yaml`, the host
  registration in the live database, and the machine-local service credential
  in `~/.config/omnigent/host.env`.
- Never delete or regenerate the host ID, remove the host registration, or run
  a new unauthenticated `omnigent host` command to work around a `401`, `409`,
  or “already registered” error. Inspect the existing systemd unit, token
  environment file, and host logs first.
- Never print, commit, or copy host tokens into the repository or command
  history. If a service needs the existing token, load it through its protected
  `EnvironmentFile`.

### Database

- The live service database is `/home/vscunha/.omnigent/chat.db`; it is not the
  small example or test database in the repository.
- Before migrations, repairs, lineage changes, or any destructive database
  action, make a SQLite-aware backup that includes the WAL state. Do not delete,
  recreate, or reset the live database, and do not edit `alembic_version` by
  hand as a shortcut.
- Check the current Alembic revision and database integrity before and after a
  migration. If the migration head is unexpected, stop and inspect the
  migration graph and service logs before changing code or data.
- Keep the service running against the existing database unless the user
  explicitly authorizes a replacement or reset.

### Workers and branch continuity

- New sessions and child workers must inherit the parent session's active
  workspace and Git branch. In this installation, Beta workers must use the
  current checkout, not a fresh `origin/main` checkout.
- Do not reset, rebase, clean, or checkout another branch in the live workspace
  to make a worker start. If isolation is necessary, create it from the
  parent's current branch, preserve the parent's edits, and remove the
  temporary worktree after the worker finishes.
- Before dispatching work, confirm the worker workspace and branch. Do not
  assume that a newly created session automatically points at the latest local
  feature branch.

## Worker checkout policy

Child sessions must inherit the parent session's active workspace and Git
branch. Do not reset worker checkouts to `origin/main`. If an isolated
worktree is required, create it from the parent branch and remove it when the
worker finishes.

## Pull requests

When you open a pull request, fill in the repo's PR template at
`.github/pull_request_template.md` (case-sensitive on Linux — note the lowercase
filename). Keep every section and checkbox row so reviewers can skim them.

- **Summary** — what changed and why.
- **Test Plan** — how you verified it.
- **Demo** — a **video or images** showing the change. Expected on contributor
  PRs for UI / frontend changes (check the "UI / frontend change" box under
  *Type of change*) so reviewers can see the new behaviour without checking out
  the branch. Use `N/A` for non-visual changes.
- **Type of change** / **Test coverage** — check all that apply (at least one
  each).
- **Coverage notes** — required if you checked "Manual verification completed"
  or "Not applicable".

Generate the description from the actual diff and this session's context — lead
with the motivation, then the change. Don't pass a `--body` that skips these
sections.

## Finishing a task

When you finish a task, print instructions to the user on how to test it: the
commands to run, the inputs to provide, or the steps to reproduce so they can
verify the result themselves. Prefer verification that is best performed by a
human, such as concrete manual behavior checks, rather than only listing unit
test commands. Don't leave the user guessing how to confirm the work — tell
them exactly what to do.

## Deprecating features

When deprecating a feature, note the version in which it is expected to be
removed so we can clean it up when that version ships. Call out the deprecation
version in code (e.g. a `@deprecated` tag or comment naming the target release)
and in the PR/commit description, so there's a clear marker to act on later.

## Code comments

Keep comments short and focused on the code, not on the change history.

- **Keep them brief** — prefer one or two lines. Avoid comments longer than
  three lines; if you need more, the code likely needs refactoring or a doc
  string, not a wall of inline commentary.
- **Describe the scenario, not the PR** — explain *what* the code handles or
  *why* it exists, in terms a future reader needs. Don't reference PR numbers,
  issue numbers, or ticket IDs (e.g. `#1646`, `fixes JIRA-123`); the scenario
  should be clear without chasing external links.

## Database query names

Application stores use `make_named_managed_session_maker` and give every
session a stable semantic operation name. The session-level name must describe
the caller's intent rather than repeat SQL syntax; use a nested
`query_name_scope` only when one transaction needs distinct names for important
subqueries. Because the named session covers implicit flush and commit, don't
add an explicit `flush()` only to make a query name observable.

## Framework-owned instructions

Keep runtime lifecycle and metadata instructions separate from portable agent
instructions:

- Agent-spec and per-request instructions are user-authored. Framework-owned
  instructions are additive runtime behavior and are appended after them in
  `omnigent/runtime/prompt.py`.
- Keep the canonical instruction text and lifecycle gate in the owning framework
  module. Harness adapters should only transport the composed instructions; do
  not duplicate policy across adapters or add lifecycle metadata to `AgentSpec`.
- If framework instructions grow beyond a small ordered list, introduce a
  structured `FrameworkInstructions` value at the prompt-composition boundary.
