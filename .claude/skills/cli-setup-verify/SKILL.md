---
name: cli-setup-verify
description: Verify the Omnigent CLI's setup/onboarding flow, terminal UI/UX, and critical user journeys in a completely isolated, reproducible loop. Drives the real `omnigent` binary through a PTY (pexpect) inside a throwaway OMNIGENT_CONFIG_HOME / OMNIGENT_DATA_DIR sandbox that never touches the user's real ~/.omnigent, captures ANSI-stripped frames for UX inspection, and proves a change is verifiable via a before→fix→after baseline diff. Load when developing or reviewing a CLI setup/onboarding/REPL/picker change (omnigent/cli.py, omnigent/onboarding/*, omnigent/repl/*, scripts/install_oss.sh), reproducing a cold-start/first-run UX bug, or confirming a fix actually lands. Several agents can run it concurrently on separate worktrees.
---

# Verifying the Omnigent CLI setup & UX in a closed loop

The Omnigent CLI's first impression is: `curl | sh` → run `omnigent` → pick a
model credential → start a session. This skill lets an agent **enter that flow,
examine the UI/UX, and prove whether a change is verifiable** — without a
browser, without real credentials, and **without ever touching the developer's
real `~/.omnigent`**.

The engine is `verify_cli.py` (next to this file). It drives the real
`omnigent` binary through a pseudo-terminal (`pexpect`) inside a throwaway
sandbox, captures what renders, runs assertions, and prints one machine-readable
`SUMMARY {json}` line.

> **The whole point is a verifiable loop**, not a one-shot check:
> 1. Run a scenario on the **unfixed** code → baseline (`--label before`).
> 2. Make the change.
> 3. Run the **same** scenario → `--label after`.
> 4. Diff the two `SUMMARY` lines. A fix is "verifiable" only if a concrete
>    check or note **flips** between the two runs. If it doesn't flip, you
>    can't prove the fix did anything — go back to step 2.

## Why this is safe (read first)

The real `~/.omnigent` here can be **many GB** (chat DB, runner logs, native
harness state). The sandbox isolates every write three ways:

- **`HOME` is redirected into the sandbox by default.** This is the load-bearing
  one. `OMNIGENT_CONFIG_HOME` / `OMNIGENT_DATA_DIR` (`omnigent/cli.py`
  `_CONFIG_HOME_ENV_VAR` / `_DATA_DIR_ENV_VAR`) redirect config + data — but the
  CLI's **diagnostics logger ignores them**: it writes a per-invocation
  `cli-*.log` under `state_dir()`, hardcoded to `Path.home()/.omnigent/logs`
  (`omnigent_ui_sdk/terminal/_config.py`). So only redirecting `HOME` keeps a
  non-help command (`config list`, the setup PTY spawns, `server stop`) from
  writing into the real home. The driver does this for you.
- `--strip-path` reduces `PATH` so `node`/`tmux`/`claude`/`codex` read as "not
  installed" → the true fresh-machine cold start.
- Ambient model keys (`ANTHROPIC_API_KEY`, …) are stripped from the child env
  unless you pass `--keep-env-creds`.

**`--inherit-home` opts out** of `HOME` isolation — use it only to reach a real
credentialed REPL via ambient `~/.claude` / `~/.databrickscfg` auth. It is
**less safe**: a non-help command then writes `cli-*.log` into the real
`~/.omnigent/logs`.

Every run **fingerprints the real `~/.omnigent` before and after** (stat-only,
no content reads): the top-level config files **and** the set of
`logs/cli-*.log` diagnostic files. A new config file/mtime *or* a new `cli-*.log`
basename trips `real_config_untouched: false`. With the default isolation that
never happens; under `--inherit-home` it correctly does — which is exactly the
violation the guard is meant to catch. If that check is ever `false`, stop and
investigate. Run `check-isolation` first to confirm the loop is safe on your
machine.

## Prerequisites

- You're in the **worktree whose code you want to test** (each parallel agent
  on its own worktree). The driver runs `omnigent` from `--repo`'s checkout.
- A Python with `pexpect` — the project's `.venv/bin/python` bundles it
  (`pexpect>=4.9` in `pyproject.toml`). Run the driver with that interpreter.
- An `omnigent` binary: the driver auto-finds `<repo>/.venv/bin/omnigent`, or
  pass `--omnigent <path>`.
- The setup / picker / help / cold-start scenarios need **no credentials and no
  harness**. Only `repl-commands` needs a working harness + credential: pass
  `--inherit-home` (ambient `~/.claude` auth) and/or `--keep-env-creds` (env API
  key) with `--agent`. It reports `skipped`, never a false pass, when the prompt
  isn't reachable.

## Quick start

```bash
REPO=/path/to/your/worktree
PY=$REPO/.venv/bin/python
DRV=$REPO/.claude/skills/cli-setup-verify/verify_cli.py

# 0. Prove the sandbox is safe on this machine (do this once).
#    HOME is isolated by default — no flag needed.
$PY $DRV --scenario check-isolation --repo "$REPO"

# 1. See exactly what a brand-new user sees on a fresh machine.
$PY $DRV --scenario cold-start --strip-path --keep-sandbox --repo "$REPO"
#    → reads the printed `artifacts` path, then `cat <that>/cold_start.txt`

# 2. Lint the top-level help (and any subcommand's).
$PY $DRV --scenario help-snapshot --repo "$REPO"
$PY $DRV --scenario help-snapshot --subcommand server --repo "$REPO"
```

Each run prints `SUMMARY {…}` and exits non-zero if any check failed (a
`skipped` scenario exits 0). Pipe to `… | grep '^SUMMARY' | python -m json.tool`
to read it.

## Scenario catalog

| Scenario | What it drives | Key checks / notes | Maps to findings |
|---|---|---|---|
| `check-isolation` | `omnigent config list` in the sandbox (no PTY) | `config_list_ran`, `sandbox_config_home_used`, `real_config_untouched` | safety gate for everything |
| `cold-start` | `omnigent setup` via PTY on a simulated fresh machine | `onboarding_rendered`, `harness_menu_present`; note `guided_default_affordance` | cold-start dead-end; missing "recommended start here" |
| `setup-snapshot` | `omnigent setup`, optional `--nav-down N` arrow steps | `menu_rendered`; saves a frame per step | picker markers/footer/alignment; narrow-terminal at 80×24 |
| `help-snapshot` | `omnigent [--subcommand] --help` (no PTY) | `help_rendered`, `no_param_leak`, `no_update_dup`; note `top_level_command_count` | `:param` leak, duplicate `update`/`upgrade`, command sprawl |
| `repl-commands` | `omnigent run <agent>` REPL, sends `/help` + `/quit` | `help_lists_commands`; note `quit_advertised` | REPL discoverability (`/help`, `/quit`) |

`--list-scenarios` prints them too. Captured frames land in the printed
`artifacts` dir as both `<name>.txt` (ANSI-stripped, for reading/asserting) and
`<name>.ansi.txt` (raw, to see real colors with `less -R`).

## The verifiable loop — a worked example

Finding: *"`server --help` leaks Sphinx `:param`/`:returns` into user help."*

```bash
# BEFORE the fix (on the unfixed code):
$PY $DRV --scenario help-snapshot --subcommand server --label before --repo "$REPO"
#   → "no_param_leak": {"ok": false, ...}      ← bug reproduced (the baseline)

# ... make the change (move :param docs into # comments) ...

# AFTER the fix:
$PY $DRV --scenario help-snapshot --subcommand server --label after --repo "$REPO"
#   → "no_param_leak": {"ok": true, "detail": "clean"}   ← flipped → fix is verifiable
```

The same shape proves the `update`/`upgrade` duplicate (`no_update_dup`), the
cold-start dead-end (`guided_default_affordance` note flips `absent`→`present`),
or REPL `/quit` discoverability (`quit_advertised` note flips `no`→`yes`). **If
the check/note doesn't flip, the fix isn't proven** — that is the signal to keep
working, and it's exactly the judgment the loop exists to force.

If a finding has no machine check yet, add one (see "Adding a scenario") so the
fix becomes provable instead of asserted.

## Examining UI/UX deliberately

- **Narrow terminal is the default.** The driver uses **80×24** — the size a
  new user's window actually is, and where banner overflow and picker
  redraw-past-the-bottom bugs appear. Re-run with `--cols 120 --rows 40` to
  compare the roomy layout; diff the two frames.
- **Read the frame, don't just trust the check.** `cat <artifacts>/cold_start.txt`
  shows the literal screen — the all-`✗` menu, the footer hint (`Esc back` at
  the root), the marker (`❯`), alignment of the status gutter. The frame *is*
  the UX evidence.
- **Compare pickers for consistency.** `setup-snapshot --nav-down 3` captures
  the harness menu as you move; eyeball marker/footer/highlight drift against
  the theme and resume pickers (different engines render differently).

## Covering all critical user journeys

This skill owns the **setup / onboarding / first-run / TUI** journeys. The repo
already has complementary CUJ coverage — use both:

- **Live setup/UX journeys → this skill's scenarios** (cold-start, setup,
  pickers, help, REPL discoverability).
- **Deeper end-to-end journeys → `tests/e2e/test_journey_*.py`** (first session
  to code, resume/disconnect, fork/explore, file upload, collaboration, …).
  Run a slice with the project's gated runner, e.g.
  `uv run --frozen --extra dev python -m pytest tests/e2e/test_journey_first_session_to_code.py -q`.
- **Reusable PTY helpers** live in `tests/e2e/omnigent/_pexpect_harness.py`
  (`spawn_omnigent_run`, `wait_for_ready`, `submit_prompt`, `await_turn_complete`,
  `clean_exit`) and the snapshot comparator in `tests/e2e/omnigent/_snapshot.py`
  — prefer extending those over re-inventing.

To drive a surface this skill doesn't script yet, spawn it by hand with the
sandbox env and the keys the driver exports (`KEY_UP`/`KEY_DOWN`/`KEY_ENTER`/
`KEY_ESC`), then `drain()` and `save_frame()` the result.

## Teardown — non-negotiable

- The driver force-kills the PTY child and its descendants, and runs
  `omnigent server stop` against the sandbox to reap any spawned background
  server. After a run, confirm nothing leaked:
  `pgrep -af "omnigent.*(server|runner|host._daemon)"` — anything bound to your
  sandbox's data dir is yours to kill.
- The sandbox temp dir is deleted unless `--keep-sandbox`. If you keep one for
  inspection, `rm -rf` it when done.
- Always drive the CLI through the driver (which redirects `HOME` + the
  config/data knobs), never a bare `omnigent setup` — that would write to the
  real `~/.omnigent`. If you pass `--inherit-home`, expect `cli-*.log` writes to
  the real `~/.omnigent/logs` and a `real_config_untouched: false` — that's the
  guard working, not a bug.

## Honesty

If you can't reach the surface under test (no harness, no credential, headless
limit), the scenario must report `skipped` — **do not claim a CUJ passed**. The
strongest evidence for a fix is a reproduced baseline (`before`) plus the flipped
`after`; report both `SUMMARY` lines, not a summary of a summary.

## Adding a scenario

Write `scenario_<name>(args, sandbox, result)` in `verify_cli.py`: drive the CLI
(reuse `pexpect.spawn(... env=sandbox.env, dimensions=(args.rows, args.cols))`,
`drain()`, `save_frame()`, the `KEY_*` constants), record findings with
`result.add(name, ok, detail)` (fails the run) or `result.notes.append(...)`
(informational, for before/after flips), register it in `SCENARIOS`, and add a
row to the catalog above. Keep one assertion per real, observable behavior so a
fix is provable as a single check flip.

## Code under test

- First-run dispatch / no-arg routing: `omnigent/cli.py` (`run`, the first-run
  plan, `_run_configure_harnesses_interactive`).
- Onboarding: `omnigent/onboarding/*` (`setup.py`, `interactive.py`,
  `configure_models.py`, `provider_selection.py`, `detected.py`).
- TUI / REPL & pickers: `omnigent/repl/*` (`_repl.py`, `_theme_picker.py`,
  `_resume_picker.py`), `omnigent/_terminal_picker_theme.py`.
- Installer: `scripts/install_oss.sh`.
