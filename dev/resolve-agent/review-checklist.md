# resolve-agent — recurring repo pitfalls checklist

A growing rubric of **mistake classes worth checking every fix against**. The
resolve-agent feeds this to its cross-vendor reviewer (AGENTS.md Step 2B.6) so the
reviewer checks for each item explicitly instead of re-deriving them every run.
These are *correctness* concerns, not style — a reviewer must surface them even
when a prompt says to skip cosmetic nits.

**Grow this file.** When a review (the pre-PR reviewer, the PR bots, or a human)
catches a class of bug that a resolve-agent fix introduced, add it here as a
one-line check so the next run catches it up front. Keep each item concrete:
what to look for, and why it's wrong.

## Tests / hermeticity

- **A "does not crash/fail" e2e must also assert a positive milestone.** A test
  that only rejects failure markers passes when the journey dies early for an
  unrelated reason and never reaches the repaired code — assert output/state
  that can only appear *past* the fixed point.
- **Guard construction-time exceptions outside the library's base hierarchy.**
  When widening an `except` around a client library call, check for errors
  raised at *client construction* that don't subclass the request-error base
  (e.g. `httpx.InvalidURL` is not an `httpx.HTTPError`) — and check every
  sibling call site on the same journey, not just the one in the traceback.
- **Env-absent tests must clear *every* relevant ambient variable.** A test
  asserting an environment-derived value is absent/None/default must clear **all**
  of the variables the code-under-test reads in its fixture — not just the obvious
  one. A fixture that clears some but leaves a sibling ambient passes on a clean
  machine and flakes in CI where that var is exported.
- **No order-dependence / shared mutable state** across tests — a test that only
  passes after another ran, or mutates a module/global without restoring it.

- **claude-sdk e2e mocks must script for parallel API calls.** The claude CLI
  opens more than one API call at turn start (main + side calls), and only the
  main call's stream events reach the executor. A mock queue with a single
  scripted entry can be consumed by a side call, silently testing a different
  failure than the journey intends — script enough identical entries that the
  main call deterministically sees the intended response.

## UI / affordances

- **Don't offer an action the code can't perform.** Flag a menu/UI option gated on
  a *resolved* value rather than on whether the action can actually act on it —
  e.g. offering to remove/clear a value that only exists ambiently and that the
  underlying edit cannot remove. Gate the affordance on "can we act on this," not
  "did something resolve."

## Environment / subprocess

- **Never replace a child's whole environment.** Passing a fresh `env=` to
  `subprocess.*` that drops the inherited environment strips `PATH`, auth, and
  proxy vars — extend `os.environ.copy()` instead of replacing it.

## Config / data safety

- **A "clear/reset" must not clobber unrelated config.** An edit that rewrites a
  config file to remove one key must preserve every other key — no full-file
  overwrite that drops the user's other settings.

- **Copy the environment with `os.environ.copy()`.** Wrapping the environ in a
  dump-style constructor (dict / json.dumps / str / repr of the whole environ)
  trips the security exfil scan on added lines; the repo idiom
  `os.environ.copy()` is equivalent and passes.

## Rollback / cleanup

- **Cleanup of an adopted resource must not destroy pre-existing user state.**
  When an operation *recreates or adopts* something that predates the request (an
  existing branch, an existing directory, an existing config), its
  rollback/cleanup path must remove only what the operation itself created —
  never force-delete the pre-existing thing (e.g. `git branch -D` on a branch the
  user owned before the call, losing unpushed commits). Check every failure path
  that shares a cleanup helper with the create-from-scratch flow.
