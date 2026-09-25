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

### Demo media

Do not commit screenshots or recordings created only as PR or issue evidence.
Upload them as GitHub attachments and embed the attachment URLs in the PR's
Demo section or issue comments. Keep local captures outside the tracked tree,
and redact private data before uploading.

Commit media only when it serves maintained documentation, product assets, or
test baselines, not merely to obtain a public demo URL. If attachment upload is
unavailable, explain the limitation and ask for help instead of committing the
files as a workaround.

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

## Session status and liveness

Session status reaches the runner on several lossy channels: its own turn
edges, the pane watcher, Claude's status file, forwarder relays and
interrupts. A dict of "the status this runner last published" once missed the
relayed edges, and the pane reaper trusted its stale `running`, so finished
panes were never reaped.

- **One source of truth.** Every status edge is recorded in `SessionStatusBook`
  (`omnigent/runner/session_status.py`) through one of its audited recorders.
  A new channel gets a new `StatusSource`; it does not get its own dict.
- **Caches are derived and owned by the recorder.** Any view of status is
  updated by the call that records the edge and cleared by the book's
  `reset`/`forget`. The wire dedup baseline records what the server heard,
  not what the session is doing.
- **A recorded status is a claim, never a veto.** A `running` can stay stale
  forever. A decision to keep a pane, process or turn alive needs first-hand
  evidence too (a live runner turn, pane output, a harness probe, a pending
  prompt) and must still conclude if the status never changes again.
  The one sanctioned claim-based hold is the runner idle watchdog's
  native-turn hold (`_native_turn_in_flight` in `omnigent/runner/app.py`). A
  recorded `running`/`waiting` keeps the runner up for at most
  `OMNIGENT_NATIVE_PANE_MAX_TURN_S` (floored at an hour) after the start of
  its episode or the turn's last first-hand evidence of work (the agent pane's
  output, a runner dispatch). A dialog the agent reported (`blocked_on`) or an
  open prompt park keeps it up for at most `OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S`
  from when it opened, whatever `OMNIGENT_NATIVE_PANE_MAX_TURN_S` is. A
  re-asserted status never renews either, and a reap ends them sooner (a
  refutation ends a recorded claim). Do not add another.
- **New native harnesses conform.** Declare `pane_reap` and `status_owner` in
  `omnigent/harness_plugins.py`; `tests/runner/test_native_pane_reap_conformance.py`
  then runs the harness through every scenario and must pass without new
  `_KNOWN_GAPS`.

The `session-status-single-source` custom-lint rule and
`tests/runner/test_session_status_single_recorder.py` enforce this. Record
through the book instead of suppressing them; a `# custom-lint: disable=`
must say what the container holds. See `docs/native-pane-reaping.md`.

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
