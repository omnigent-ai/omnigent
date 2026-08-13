# Server ↔ Runner Backwards-Compatibility CI

This document describes the two compat CI configurations and how to run them
locally or in a pipeline.

## Overview

Server and runner are deployed independently. A newer server may run alongside
an older runner (Config 1), or a newer runner alongside an older server
(Config 2). The smoke tests in
`tests/e2e/test_server_runner_compat_smoke.py` guard both orderings with a
minimal end-to-end turn.

## Env vars

| Variable | Component | Purpose |
|---|---|---|
| `OMNIGENT_COMPAT_SERVER_PYTHON` | Server | Venv python for the server subprocess. Set to `<old-venv>/bin/python` to pin the server to an older build. Unset → uses `sys.executable` (HEAD). |
| `OMNIGENT_COMPAT_SERVER_VERSION` | Server | Version string the workflow pinned (e.g. `"0.9.0"`). Used as a backstop when `/api/version` is unreachable and to cross-check the reported version. |
| `OMNIGENT_COMPAT_RUNNER_PYTHON` | Runner/host | Venv python for the runner and host subprocesses. Set to `<old-venv>/bin/python` to pin runner/host. Unset → uses `sys.executable` (HEAD). |
| `OMNIGENT_COMPAT_RUNNER_VERSION` | Runner/host | Version string the workflow pinned (e.g. `"0.9.0"`). The runner/host expose no `/api/version`; this var is the only version source for `min_runner_version` skips. |

The two knobs are **orthogonal**: each spawn site consults its own variable.
You can pin server or runner/host independently; you should never need to set
both at once.

## Config 1 — new server, old runner

The server is upgraded first; runners roll out more slowly.

```sh
# Build a venv with the last released version.
python -m venv /tmp/old-runner-venv
/tmp/old-runner-venv/bin/pip install omnigent==0.9.0

# Run the compat smoke tests.
OMNIGENT_COMPAT_RUNNER_PYTHON=/tmp/old-runner-venv/bin/python \
OMNIGENT_COMPAT_RUNNER_VERSION=0.9.0 \
  pytest tests/e2e/test_server_runner_compat_smoke.py -v
```

What the test harness does:
- Spawns the server with `sys.executable` (HEAD).
- Spawns the runner with `/tmp/old-runner-venv/bin/python` from an empty CWD
  so the old venv's installed `omnigent` resolves instead of the worktree.
- Runs `test_new_server_old_runner_compat_smoke` (no `min_server_version`
  guard — it must run against any server).
- Skips `test_new_runner_old_server_compat_smoke` — the runner is older here
  so the "new runner" scenario is not exercised.

## Config 2 — new runner, old server

Runners are upgraded first; the server rolls out more slowly.

```sh
# Build a venv with the last released version.
python -m venv /tmp/old-server-venv
/tmp/old-server-venv/bin/pip install omnigent==0.9.0

# Run the compat smoke tests.
OMNIGENT_COMPAT_SERVER_PYTHON=/tmp/old-server-venv/bin/python \
OMNIGENT_COMPAT_SERVER_VERSION=0.9.0 \
  pytest tests/e2e/test_server_runner_compat_smoke.py -v
```

What the test harness does:
- Spawns the server with `/tmp/old-server-venv/bin/python` from an empty CWD.
- Spawns the runner with `sys.executable` (HEAD).
- `test_new_runner_old_server_compat_smoke` carries
  `@pytest.mark.min_server_version("0.9.0")`.  Any server version ≥ 0.9.0
  runs the test; older servers skip it (the baseline for the session-init
  envelope and `/api/version` probe).

## CWD isolation

`python -m omnigent...` adds CWD to `sys.path[0]`, so launching from the repo
worktree would import the branch's `omnigent/` package and shadow the pinned
old install. Both compat helpers create a stable empty temporary directory
(`compat_server_cwd()` / `compat_runner_cwd()`) for the pinned subprocess CWD
when compat mode is active.

## Tripwire: version cross-check

`resolve_server_version()` reads `GET /api/version` from the live server and
reconciles it against `OMNIGENT_COMPAT_SERVER_VERSION`. A mismatch (e.g.
the worktree's server shadowed the pinned install despite the CWD fix) raises
`RuntimeError` immediately — the cross-check fires on the first test in any
suite that resolves `server_version`, not just the compat file.

## Adding new compat guards

When you add a feature that changes the server ↔ runner contract:

1. **Unit-level**: add a `_version_supports_<feature>` predicate in
   `omnigent/runner/app.py` (see `_version_supports_waiting_status`) and unit-
   test the predicate in `tests/runner/test_<feature>_compat.py`.
2. **E2E marker**: mark tests that require the new server behavior with
   `@pytest.mark.min_server_version("X.Y.Z")` or
   `@pytest.mark.min_runner_version("X.Y.Z")` so compat CI skips them on
   older builds.
3. **Smoke breadcrumb**: if the protocol change could silently break a turn
   end-to-end, extend `test_server_runner_compat_smoke.py` with a dedicated
   test that reproduces the failure scenario.
