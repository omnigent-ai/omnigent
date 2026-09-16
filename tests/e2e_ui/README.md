# UI shard timings

CI keeps ten serial pytest shards. Each reads `durations.json` from the same
checkout, applies the normal marker/keyword filters, and assigns the longest
estimated tests to the least-loaded shard. Test order within a shard stays in
collection order. No tests are dropped or moved out of the PR gate.

Weights include setup, call, and teardown, including reported retry attempts.
New or renamed tests use the selected suite's 75th-percentile duration (at least
one second). With no matching history, assignment falls back to round-robin.
Invalid snapshots fail collection rather than silently changing the plan.

The estimates are approximate: shared fixture costs can move between tests or
be repeated on another shard, and retry delays, runner setup, queueing, and
security-gate waits are outside the recorded phase durations. Compare actual
workflow and test-step elapsed times as well as predicted loads.

## Refresh the snapshot

Every shard uploads an `e2e-ui-timings-<run>-<attempt>-shard<N>` artifact, even on
failure. Its JSONL contains the selected IDs, full collection digest/count,
snapshot digest, run metadata, every reported phase, and final pytest status.
Partial artifacts remain useful for diagnosis but cannot update the snapshot.

Download **all ten artifacts from one successful run and attempt** into a fresh
directory, then generate and review a snapshot:

```sh
gh run download RUN_ID --repo omnigent-ai/omnigent \
  --pattern 'e2e-ui-timings-RUN_ID-ATTEMPT-shard*' --dir /tmp/ui-timings
uv run --no-sync python -m tests.e2e_ui.sharding \
  /tmp/ui-timings/*/ui-timings.jsonl --output tests/e2e_ui/durations.json
uv run --no-sync pytest tests/github/test_ui_sharding.py -q
```

The builder rejects mixed runs/snapshots/filters, missing or duplicate shards,
failed or unfinished pytest runs, and assignments that do not cover collection
exactly once. Skipped tests do not supply duration estimates. Commit the result
so all subsequent shards share one immutable snapshot. Refresh after significant
suite changes or when observed shard times drift; a scheduled full-suite run
also supplies estimates for nightly-only cases.

## Verify a change

Open the PR's **E2E UI Tests** workflow. All ten shard jobs should pass. Each
pytest log prints its assigned count and the ten estimated loads. Download its
timing artifacts and run the refresh command to independently validate coverage.
Compare the slowest **Run UI e2e tests** step, workflow elapsed time, and
sum of shard test-step times with the baseline; inspect reruns and setup delays
before attributing the difference to scheduling. Several runs are needed to
establish a stable improvement.

To preview one shard locally without starting browsers:

```sh
uv run --no-sync pytest tests/e2e_ui --collect-only -q \
  -m 'not visual and not nightly' \
  --ui-duration-file=tests/e2e_ui/durations.json --splits=10 --group=1
```
