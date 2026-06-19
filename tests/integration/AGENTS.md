# tests/integration/ — per-harness journey suite

Real-server, real-LLM journeys run once per wrapped harness. These
replace the per-harness signal lost when `tests/inner/test_integration.py`
was deleted with the legacy in-process runtime (PR #1800), but they
exercise the production server/runner path, not the legacy one.

Not to be confused with `tests/server/integration/`, which is the
mock-LLM server integration suite that runs in the default CI matrix.

## How it runs

- Excluded from the default `pytest` run (`--ignore=tests/integration`
  in pyproject.toml) and additionally gated on `--integration`.
- One harness per invocation, selected by `--harness` (no default;
  must be one of `claude-sdk`, `codex`, `openai-agents`), model pinned
  by `--model`.
- CI: one nightly.yml matrix leg per harness; `claude-sdk-debug.yml`
  for focused claude-sdk iteration with `-k`.

Local examples (the harness CLI must be installed for claude-sdk /
codex; missing CLIs skip with a clear reason):

```bash
pytest tests/integration/ --integration --profile <name> --llm-api-key $KEY \
    --harness claude-sdk --model databricks-claude-sonnet-4-6 -v

pytest tests/integration/ --integration --profile <name> --llm-api-key $KEY \
    --harness openai-agents --model databricks-gpt-5-4-mini -v

pytest tests/integration/ --integration --profile <name> --llm-api-key $KEY \
    --harness codex --model databricks-gpt-5-5 -v
```

## Journeys

| File | Invariant |
|---|---|
| `test_smoke.py` | Single-turn marker echo (basic harness liveness) |
| `test_multi_turn.py` | Three-turn context retention across dispatches |
| `test_client_tools.py` | Tunneled client-tool results thread into the next turn's context |
| `test_sharing.py` | An EDIT collaborator's turn sees the owner's context and completes |

One journey per file on purpose: `--dist=loadscope` groups by module,
so separate files parallelize across xdist workers (each worker gets
its own session-scoped live server, same as `tests/e2e/`).

## Authoring rules

- Keep every test under the CI per-test `--timeout=180` cap PER
  ATTEMPT (turn polls are capped at 50s for this reason). Do not add
  blanket `llm_flaky`/`flaky` markers; the one sanctioned exception is
  the conftest's codex-only rerun (bursty empty-turn flake, #544/#599
  class), which is safe because attempts stay under the cap.
- Prompts must be imperative and assertions must check literal
  markers (`uuid` hex), never just "some text came back".
- New harness? Add a nightly.yml matrix leg and extend
  `_SUPPORTED_HARNESSES` in `conftest.py`.

## Mock-LLM mode (also runs in the default suite)

When invoked with NO `--llm-api-key`, this directory's `--integration`
gate is lifted (`conftest.py::pytest_collection_modifyitems`) and the
tests run against the always-on mock LLM server instead of a real
gateway. `using_mock_llm` is True and `_is_mock_mode(config)` is the
signal. The same files run in TWO CI job families: `Pytest
(integration-mock)` (mock) and `Integration (claude-sdk|codex|
openai-agents)` (real LLM, passes `--llm-api-key`).

Two rules keep the two modes correct:

- **`mock_only` marker** — tests whose mock LLM is scripted with a
  fixed tool-call sequence (e.g. the scripted server→client round-trip
  tests) CANNOT run against a real LLM: it would 401 on the mock base
  URL and could never reproduce the scripted call_ids/markers. Mark
  those modules `pytestmark = pytest.mark.mock_only`; the central gate
  in `conftest.py` skips them when a real `--llm-api-key` is supplied.
  Do NOT mark dual-mode journeys (`test_smoke` / `test_multi_turn` /
  `test_sharing` / `test_client_tools`) — they use the `default` queue
  and are designed to run in both modes.
  - A `if mock_llm_server_url is None: pytest.skip(...)` guard inside a
    test body is DEAD CODE: the `mock_llm_server_url` fixture is "always
    started regardless of --llm-api-key", so it never yields `None` and
    the skip never fires. Use the `mock_only` marker, not that guard.
- **Central queue reset** — `conftest.py` has an autouse,
  function-scoped `_reset_mock_llm_between_tests` fixture that clears the
  shared (session-scoped) mock server queues before and after every
  test. The mock server falls back to a default response when a queue is
  exhausted or keyed for another agent, so without this reset a scripted
  test leaks responses into its siblings. Do NOT add a per-file reset
  fixture — the central one covers the whole directory.
