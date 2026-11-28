# UI timing artifacts

Every UI CI shard uploads `e2e-ui-timings-<run>-<attempt>-shard<N>` with one or
more `ui-timings*.jsonl` files, retained for 14 days. A checked-in snapshot of
these measurements, `durations.json`, balances the CI shards. Timings never
exclude tests: every collected test belongs to exactly one shard.

Each file contains:

- A `plan` record with selected test IDs, shard, run/attempt/commit, and filters.
  Sharded runs also record `sharding`: the algorithm, that shard's estimated
  seconds, and how many collected tests had no snapshot entry.
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

Under pytest-xdist (`-n`), each worker writes `ui-timings-<worker>.jsonl` beside
the requested path, and the controller writes none. Every worker's `plan` lists
the whole shard selection and names its `worker`; its `phase` records cover only
the tests that worker ran, so combine all worker files for one shard.

Download all shard files for a specific run and attempt into a fresh directory:

```sh
gh run download RUN_ID --repo omnigent-ai/omnigent \
  --pattern 'e2e-ui-timings-RUN_ID-ATTEMPT-shard*' --dir /tmp/ui-timings
```

To verify, open the PR's **E2E UI Tests** workflow and check that each shard has
an uploaded timing artifact. In a downloaded file, check that every selected
ID has phase records and that the final record reports exit status 0 for a
successful nonempty shard. Compare total setup/call/teardown time across
shards and investigate retries separately before refreshing the snapshot.

To record timings locally:

```sh
uv run --no-sync pytest tests/e2e_ui -k TEST_NAME \
  --ui-timing-output=/tmp/ui-timings.jsonl
```

## Scheduling

`--splits` and `--group` place the longest estimated tests on the least-loaded
shard, then keep collection order inside the shard. Ties are deterministic.
Assignment happens after marker and keyword filtering. Unsharded runs do not
read the snapshot. A missing or malformed snapshot fails a sharded run.

Tests absent from the snapshot use its median duration. Entries for tests that
were not collected are ignored, so a stale snapshot cannot add or drop a test.

Same-repo CI runs two pytest-xdist workers per shard (`E2E_UI_PARALLELISM`,
`--dist=loadscope`). Fork PRs leave that variable unset and run each shard
serially. The snapshot balances the sum of test time and does not change how
many workers a shard starts.

Refresh from several complete successful runs:

```sh
python -m tests.e2e_ui.update_durations /tmp/ui-run-a /tmp/ui-run-b
```

The updater sums setup, call, and teardown, takes the median, and skips skipped
or retried tests. It combines the worker files from one shard. It rejects
incomplete or failed runs, missing or duplicate shards, and overlapping plans.
If the reference-policy check rejects a node id in the regenerated file, delete
that entry. The test still runs, estimated at the median.
