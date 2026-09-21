# Prepared reproduction environment

CI starts the product server, runner and mock model server in a persistent
sandbox before repro-agent launches. It configures both real native CLIs with
mock providers and isolated product/CLI state. The workflow owns their lifetime;
they survive the agent CLI disconnecting and individual shell calls ending.

Run each journey in the foreground through the connection wrapper:

```sh
python -m dev.repro_env exec -- python -m pytest \
  tests/e2e_ui/messages/test_native_claude_render_parity.py::test_native_claude_message_render_parity \
  --ui-skip-build --video=on --output=recordings/native-claude
```

Use `native_codex_mock_session` or `native_claude_mock_session` in authored UI
tests. Existing fixtures attach to the prepared server and runner, and
`mock_llm_server_url` addresses its model server. They do not provision another
runner or decide the model backend from ambient credentials.

Arbitrary Python/Playwright commands also work with the wrapper. They receive
`OMNIGENT_REPRO_SERVER_URL`, `OMNIGENT_REPRO_MODEL_URL`, and
`OMNIGENT_REPRO_RUNNER_ID`. These URLs are valid only inside that invocation;
reuse session IDs across invocations, not the temporary URLs. HTTP, SSE and
terminal WebSockets all use the same product endpoints. Put shared files in the
worktree, since `/tmp` is private to each sandbox.

Script model responses using the existing helpers in `tests/e2e_ui/conftest.py`
(`configure_mock_llm`, `set_fallback_mock_llm`, `reset_mock_llm`). Responses may
include text, tool calls, delays, errors or streaming interruptions. Session
creation and launch options remain the regular product API. Mock state is shared
within one reproduction attempt; configure it before each journey and run
journeys sequentially.

Record before performing the reported actions and close the browser context to
finalize video, including on assertion failure. A crash, missing reply, or stuck
approval can be reproduction evidence. A successful canned reply is only a
connectivity check; the authored test decides whether the reported bug occurred.
These mocks validate native integration, not live-provider/model behavior.

`python -m dev.repro_env status` prints startup status. Inspect
`.omnigent/repro-env/` for process logs, product logs under `data/`, provider
configuration, database and model request statistics. Connection failures must
be reported with their actual diagnostics; do not substitute callbacks or fake
terminal output and claim a real native turn.

Shutdown also saves the mock's captured request bodies. These cover requests
since the last `reset_mock_llm`; save them before resetting if an earlier
journey's model traffic is needed as evidence.

The workflow stops the environment after session completion and recording
normalization, then bundles diagnostics even on failure. A six-hour lease bounds
its lifetime if normal cleanup cannot run. `serve` is a foreground supervisor
intended for the workflow's persistent sandbox; starting it as a background job
in an agent shell does not give it that lifetime.
