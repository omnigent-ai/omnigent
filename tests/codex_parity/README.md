# Codex Parity Tests

This suite verifies Omnigent's Codex integration by running the real boundary
we care about:

```text
Omnigent CodexExecutor
  -> real codex app-server process
  -> mock OpenAI Responses API
```

The important choice is that the tests do not mock the Omnigent-to-Codex API.
They start a real Codex CLI and only replace the upstream model endpoint. That
means the test covers Codex app-server JSON-RPC behavior, Codex request
serialization, retry notifications, streaming notifications, and dynamic tool
round trips.

## Architecture

```text
pytest
  |
  | starts
  v
Rust sidecar: tests/codex_parity/sidecar
  |
  | uses upstream Codex test helper crate
  v
core_test_support::responses / WireMock
  ^
  | /v1/responses
  |
real codex app-server
  ^
  | JSON-RPC app-server protocol
  |
Omnigent CodexExecutor
```

The Rust sidecar exists because Codex's mock Responses API helpers are Rust
test-support code in the public Codex repository. Rather than reimplementing
that mock in Python, the sidecar pulls the upstream test-support crate directly
through Cargo:

```text
core_test_support = { git = "https://github.com/openai/codex.git", rev = "..." }
```

That keeps the fake Responses wire format aligned with Codex upstream. Pytest
still owns the test scenarios and assertions; the sidecar only starts WireMock,
serves queued SSE fixtures, and reports captured requests.

The revision is pinned in `tests/codex_parity/sidecar/Cargo.toml` so the parity
harness is reproducible without requiring a checked-in Codex submodule. Updating
the upstream fixture implementation is a normal Cargo dependency bump: change the
Codex `rev`, refresh `Cargo.lock`, and run the parity tests.

## Fixture Flow

Each test passes a list of model responses to the sidecar:

```python
sidecar = codex_responses_sidecar(
    [
        [
            ev_response_created("resp-1"),
            ev_assistant_message("msg-1", "hello"),
            ev_completed("resp-1"),
        ]
    ]
)
```

Each inner list becomes one SSE response body. Codex consumes one body per
`POST /v1/responses` request. Multi-turn scenarios enqueue multiple inner
lists, for example a dynamic tool call followed by the assistant's final
answer after Omnigent returns the tool result.

The sidecar prints one JSON `ready` line with a `base_url`. The pytest fixture
passes that URL into `CodexExecutor` using the existing gateway override path,
so Codex sends model traffic to the sidecar instead of OpenAI.

After a turn, pytest asks the sidecar for captured requests over a small JSONL
stdin/stdout protocol:

```json
{"op": "requests", "min": 1, "timeout_ms": 5000}
```

The response includes stable fields that are useful for parity assertions:
request path, selected headers, and JSON body.

## Coverage

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

Not yet represented here: upstream SDK-only app-server tests for lifecycle,
login, approvals, goal operations, steer/interrupt, local/remote image input,
and skill input. Those APIs do not have a direct Omnigent `CodexExecutor`
surface yet, so they need either executor-facing analogs or a separate SDK
compatibility harness before they can be one-for-one parity tests.

## Running

Run against the Codex CLI on `PATH`:

```bash
pytest tests/codex_parity --codex-parity -v
```

Run against one explicit binary:

```bash
pytest tests/codex_parity --codex-parity --codex-bin "$(which codex)" -v
```

Compare multiple Codex versions:

```bash
pytest tests/codex_parity \
  --codex-parity \
  --codex-bin /path/to/codex-old \
  --codex-bin /path/to/codex-new \
  -v
```

You can also set `CODEX_TEST_BINS` to an `os.pathsep`-separated list.

At Databricks, use the internal PyPI proxy when syncing the Python test
environment:

```bash
uv --no-config run --frozen \
  --default-index https://pypi-proxy.cloud.databricks.com/simple/ \
  --extra dev \
  pytest tests/codex_parity --codex-parity --codex-bin "$(which codex)" -q
```

## Why This Shape

Mocking the Omnigent-to-Codex API would test our assumptions about Codex's
app-server protocol. This suite instead lets Codex define that contract by
running the actual CLI/app-server implementation. Only the final network hop is
mocked, which gives us stable, deterministic tests while still catching
protocol drift between Omnigent and Codex.
