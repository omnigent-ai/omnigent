# UI timing artifacts

Every UI CI shard uploads `e2e-ui-timings-<run>-<attempt>-shard<N>` with a
`ui-timings.jsonl` file, retained for 14 days. This only records measurements;
the existing round-robin sharding, test selection, order, and retries stay the
same. Timings are not used to schedule or exclude tests.

Each file contains:

- A `plan` record with selected test IDs, shard, run/attempt/commit, and filters.
- A `phase` record for every reported setup, call, and teardown, including
  outcome, duration in seconds, and retry attempt (zero-based).
- A `finish` record with pytest's exit status, when pytest finishes normally.

Records are appended as tests run, and artifacts upload even on failure.
Interrupted processes may leave partial records with no `finish`, and the final
JSON line may be incomplete. Inspect completion and coverage before treating a
run as a full timing baseline.
Reported phase durations exclude runner setup, queueing, and retry sleeps.
Shared fixture setup/teardown is attributed to the test that triggers it.

The initial `plan` record supplies the schema version for the whole file. Its
`commit` field is `GITHUB_SHA`: on PR runs, this is the tested merge commit.

Download all shard files for a specific run and attempt into a fresh directory:

```sh
gh run download RUN_ID --repo omnigent-ai/omnigent \
  --pattern 'e2e-ui-timings-RUN_ID-ATTEMPT-shard*' --dir /tmp/ui-timings
```

To verify, open the PR's **E2E UI Tests** workflow and check that each shard has
an uploaded timing artifact. In a downloaded file, check that every selected
ID has phase records and that the final record reports exit status 0 for a
successful nonempty shard. Compare total setup/call/teardown time across
shards and investigate retries separately before choosing scheduling changes.

To record timings locally (the option requires serial pytest):

```sh
uv run --no-sync pytest tests/e2e_ui -k TEST_NAME \
  --ui-timing-output=/tmp/ui-timings.jsonl
```

## Native delegation journeys

Use `native_claude_mock_session` and `agents/test_native_delegation.py` as the
local native reference. It discovers the CLI's advertised `Agent`/`Task` schema,
executes that tool, checks the matching parent transcript result and child
`tool_use_id` link, and clicks the exact Agents row to reach the child.
A synthetic `external_subagent_start` only tests the forwarder/server boundary.

`configure_mock_llm(..., match=nonce, required_tools=[tool_name])` returns an
independent queue key. Check `/mock/queues` before triggering; inspect
`/mock/selections` alongside `/mock/requests` afterward. A selection records what
the mock dequeued, not proof that a blocked/truncated response reached the CLI.
Repeated selectors and explicit keys replace their existing queue. Use tool guards on parent and worker
queues so title requests containing the same nonce cannot consume their replies.

`native_driver.py` provides accepted-message and synthetic child-event helpers,
exact call/child observations, and `navigate_to_child` (no navigation fallback).
Child summaries do not expose task IDs. Use `wait_claude_completion` to require
the exact native call's reply or completed notification, then check the linked
child's expected reply and settled UI state. An idle child alone is not proof
of successful completion.
Start `LogWindow` immediately before the action and finish after the observed
outcome; it matches session and optional turn fields together inside that byte
window. A missing log match establishes absence only in that file and interval.

Set `OMNIGENT_E2E_RECORD_DIR` to record sync or async Playwright contexts; close
them after asserting the settled visible state. Keep local provider configuration
isolated with `OMNIGENT_CONFIG_HOME` and `CLAUDE_CONFIG_DIR`. The native test needs
a CLI that honors the mock endpoint; a managed gateway override is a setup
limitation, not evidence that delegation failed.

```sh
uv run --no-sync pytest tests/e2e_ui/agents/test_native_delegation.py \
  tests/e2e_ui/recording/test_record_video.py --browser chromium
```
