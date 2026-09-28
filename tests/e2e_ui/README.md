# UI timing artifacts

Every UI CI shard uploads `e2e-ui-timings-<run>-<attempt>-shard<N>` with a
`ui-timings.jsonl` file, retained for 14 days. A checked-in snapshot of these
measurements balances work across the ten CI shards. Timings never exclude
tests; every collected test still belongs to exactly one shard.

Each file contains:

- A `plan` record with selected test IDs, shard, run/attempt/commit, and filters.
  For sharded runs, `sharding` includes the algorithm, estimated shard duration,
  and the number of collected tests without a known estimate (across all shards).
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
shards and investigate retries separately before refreshing the estimates.

To record timings locally (the option requires serial pytest):

```sh
uv run --no-sync pytest tests/e2e_ui -k TEST_NAME \
  --ui-timing-output=/tmp/ui-timings.jsonl
```

## Scheduling and refreshing estimates

`--splits` and `--group` assign the longest estimated tests first to the shard
with the least estimated work. Ties are deterministic. Assignment happens
after marker and keyword filtering, and preserves collection order within
each shard. Unsharded local runs do not read the snapshot. New or renamed tests
use the snapshot's median duration; stale entries cannot select removed tests.

`durations.json` contains median setup + call + teardown seconds from successful
CI runs, with source run IDs and tested commits. Estimates affect placement only:
fixtures, assertions, retries, and real observation windows are unchanged.
A missing or malformed snapshot fails a sharded run explicitly.

Download every shard from several recent successful runs into separate
directories, then refresh and commit the snapshot:

```sh
python -m tests.e2e_ui.update_durations /tmp/ui-run-a /tmp/ui-run-b
uv run --no-sync pytest tests/github/test_ui_sharding.py tests/github/test_ui_timings.py -q
```

The updater rejects incomplete or failed runs, missing/duplicate shards, and
overlapping test plans. Skipped and retried tests do not contribute estimates.
Use successful PR runs for ordinary coverage; nightly-only cases can use the
default estimate until successful nightly timings are included. Review the
snapshot diff and use `--ui-shard-durations=PATH` to try a candidate locally.

Validate a scheduling change by collecting the unsharded suite and each of its
ten shards with `--collect-only --ui-timing-output=PATH`, using CI's marker
expression (`not visual and not nightly`, or `not visual` for scheduled runs).
The shard plans must be disjoint and their union must match the unsharded plan.
Compare actual workflow completion and total runner time over multiple CI runs;
estimated balance alone does not validate fixture placement or runtime savings.
