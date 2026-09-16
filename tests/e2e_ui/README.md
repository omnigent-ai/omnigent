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
Interrupted processes may leave partial records with no `finish`; inspect
completion and coverage before treating a run as a full timing baseline.
Reported phase durations exclude runner setup, queueing, and retry sleeps.
Shared fixture setup/teardown is attributed to the test that triggers it.

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
