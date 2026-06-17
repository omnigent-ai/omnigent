# Codex Parity Coverage

This suite exercises Omnigent's Codex boundary with a real Codex CLI and
Codex's upstream mock Responses API helpers.

Current executor-observable parity targets:

- `sdk/python/tests/test_app_server_run.py`
  - mock Responses request path/model/input
  - explicit token usage crossing the app-server boundary
  - last unknown-phase message selection
  - final-answer phase preference
  - commentary-only output not becoming the final response
  - failed Responses events surfacing as turn errors
- `sdk/python/tests/test_app_server_streaming.py`
  - text delta routing and completed-turn response
- selected request-routing behavior from `codex-rs/core/tests/suite/*`
  - dynamic tool call/result round trip through real Codex app-server

Run locally:

```bash
pytest tests/codex_parity --codex-parity --codex-bin "$(which codex)" -v
```

To compare multiple Codex versions, repeat `--codex-bin` or set
`CODEX_TEST_BINS` to an `os.pathsep`-separated list.

Not yet represented here: upstream SDK-only app-server tests for lifecycle,
login, approvals, goal operations, steer/interrupt, local/remote image input,
and skill input. Those APIs do not have a direct Omnigent `CodexExecutor`
surface yet, so they need either executor-facing analogs or a separate SDK
compatibility harness before they can be one-for-one parity tests.
