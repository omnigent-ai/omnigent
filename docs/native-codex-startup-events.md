# Native Codex startup events

The current runner-owned, direct-tmux `omnigent codex` launch path emits `event_name=client_startup` records through the existing
optional debug-log sink (`source=cli`) and writes `client_startup {JSON}` lines
in its normal CLI diagnostics log. The startup integration attaches the uploader to its content-free logger;
the CLI's general diagnostics stay local. Startup upload records remain enabled
when the CLI verbosity is WARNING or ERROR; local file/stderr handlers still
respect their configured level.

Each attempt has a fresh UUID `attempt_id`, even when resuming the same session.
The start record can have no session ID. `session_resolved` and later records
carry `session_id`, allowing joins to runner and server logs without matching
credential tokens. Attempt IDs belong in logs, not metric labels.

The structured `attributes` are:

- `schema_version`: `1`.
- `event`: one of the milestones below.
- `attempt_id`, `harness` (`codex-native`), and `launch_kind` (`create` or `resume`).
- `start_boundary`: `omnigent_cli_entry` or `wrapper_entry`.
- `started_at_unix_ms`: wall-clock timestamp of that start boundary.
- `elapsed_ms`: cumulative monotonic duration since that boundary.
- `exit_code`: present on `terminal_attach_exited`.

`launch_started` is emitted after logging is configured and the Codex command
callback is selected. Its record timestamp is **not** time zero: use
`started_at_unix_ms` for cohort selection and the completion's `elapsed_ms` for
latency. Help and argument parsing failures before the callback are not attempts.
Without a wrapper context, the start boundary is Python `main()` entry, excluding
interpreter startup and imports before `main()`.

| Event | Observed boundary |
| --- | --- |
| `launch_started` | An instrumented Codex command callback begins. |
| `session_resolved` | Session creation returns an ID, or the existing session is fetched. |
| `runner_requested` | Immediately before requesting launch/reuse of the runner. |
| `runner_connected` | The CLI's online-status wait returns successfully. This is the client's observation, not the tunnel's own connection timestamp. |
| `session_runner_bound` | The session-to-runner bind request completes successfully. |
| `terminal_available` | A lookup returns the running native terminal resource. An ensure request alone does not emit this event. |
| `initial_prompt_submitted` | The optional initial prompt HTTP request completes. It does not establish turn acceptance or model output. |
| `terminal_attach_started` | The direct tmux attach subprocess is created. This does not establish that attach succeeded or rendered anything. |
| `terminal_attach_exited` | That subprocess exits; `exit_code` preserves immediate attach failures and normal later exits. |
| `launch_failed` | An exception, including timeout, interrupts setup before attachment starts. |
| `launch_cancelled` | Setup is interrupted by cancellation before attachment starts. |
| `launch_incomplete` | The command returns before attachment starts, for example an empty resume picker. |

Milestones are emitted at most once per attempt. Resuming an already-running
terminal legitimately skips runner creation, connection wait, and binding.
There is no generic `launch_succeeded` event: choose the precise endpoint required
by the KPI. A terminal can be available yet fail to attach. Later runtime errors
do not retroactively change the startup outcome. Abrupt process termination may
leave a start without a completion; keep these attempts in the denominator.

## Wrapper exec handoff

An upstream launcher can include its own preparation in the elapsed duration by
setting `OMNIGENT_STARTUP_CONTEXT` immediately before replacing itself with
Omnigent using `os.execvpe` (or an equivalent same-process exec):

```json
{
  "version": 1,
  "attempt_id": "00000000-0000-4000-8000-000000000001",
  "elapsed_ms": 2500.0,
  "started_at_unix_ms": 1800000000000.0,
  "handoff_monotonic_ns": 123456789000,
  "pid": 12345
}
```

The wrapper chooses a new UUID per invocation, captures its start wall time and
monotonic clock together, then records the elapsed offset and handoff monotonic
clock together immediately before exec. It should document its exact start
boundary; `wrapper_entry` does not imply shell invocation or process creation.

Omnigent consumes and removes the variable at `main()` entry. It accepts version
1 only, validates fields and the UUID, requires the current PID, and rejects a
future handoff clock. The elapsed offset plus local monotonic time since handoff
includes time spent replacing the process and importing Omnigent. No monotonic
clock is compared across machines or used by the host/runner. Spawned children
with a different PID cannot adopt the context. Invalid contexts fall back to a
fresh local attempt and never break startup. The context carries no credentials.

## Measuring terminal availability

For a table exposing the debug-log schema, aggregate by attempt first. Select
cohorts by `started_at_unix_ms`, and keep completed and uncompleted counts together:

```sql
WITH attempts AS (
  SELECT
    attributes['attempt_id'] AS attempt_id,
    attributes['start_boundary'] AS start_boundary,
    min(CAST(attributes['started_at_unix_ms'] AS DOUBLE)) AS started_at_unix_ms,
    max(CASE WHEN attributes['event'] = 'launch_started' THEN 1 ELSE 0 END) AS started,
    min(CASE WHEN attributes['event'] = 'terminal_available'
      THEN CAST(attributes['elapsed_ms'] AS DOUBLE) / 1000 END) AS terminal_available_s,
    max(CASE WHEN attributes['event'] IN
      ('launch_failed', 'launch_cancelled', 'launch_incomplete') THEN 1 ELSE 0 END) AS incomplete
  FROM debug_logs
  WHERE event_name = 'client_startup'
    AND attributes['schema_version'] = '1'
    AND attributes['harness'] = 'codex-native'
    AND attributes['launch_kind'] = 'create'
  GROUP BY attributes['attempt_id'], attributes['start_boundary']
)
SELECT start_boundary,
  count(*) AS attempts,
  sum(started) AS observed_starts,
  count(terminal_available_s) AS available,
  sum(incomplete) AS incomplete_before_attach,
  percentile(terminal_available_s, 0.9) AS terminal_availability_p90_s
FROM attempts
GROUP BY start_boundary
```

Apply the desired time window to `started_at_unix_ms`. Missing completions remain
in `attempts` but cannot enter the percentile; report their count/rate alongside
latency. Do not mix wrapper and standalone start boundaries or add phase p90s.
Delivery is best-effort through the existing bounded asynchronous log sink.

## Remaining visibility gap

These events measure launch-to-terminal-availability, **not** command-to-visible
terminal or first visible answer. Direct tmux attach inherits the real terminal;
Python does not observe its output. This change adds no PTY wrapper, terminal
scraping, first-answer inference, or direct-Codex instrumentation. A comparable
user-visible measurement still needs a client output observer or vendor-provided
render/response notifications, plus an equivalent direct-launch baseline.
