# Native pane reaping

A native harness session (claude, codex, cursor, ...) runs its vendor CLI in a
tmux pane next to per-session sidecars: a forwarder or bridge task, the
tool/comment relay, and for codex and opencode a vendor server. The runner's
pane reaper (`omnigent/terminals/pane_reaper.py`) closes a pane and releases
its sidecars once the session has been idle and unattended for an idle window.
The next message re-creates both, and the vendor CLI resumes its conversation.
A model picked for a cursor, kiro or devin session while its pane is reaped is
saved by the server and applied by that relaunch.

## Which panes

The harness registry decides. A pane is offered only when its provider
declares `pane_reap="reap"` and the pane carries that harness's resource role.
kimi is exempt (it records no resumable chat id, so a reaped pane would lose
context). Undeclared community harnesses and role-less panes are skipped and
logged once. Sidecars left running with no pane (a terminal DELETE during a
turn, a crashed pane) are offered as `runtime` rows.

## When a pane is reaped

A pane is spared outright while any of these holds:

- a runner turn is live, or messages are still queued for it (for one idle
  window, then they are left buffered for the next turn and a WARNING is
  logged);
- a relayed tool call is running;
- a human can answer something: a runner ASK, a prompt a harness mirror
  surfaced, a dialog the agent reported (`blocked_on`), claude's approval
  marker, or a prompt the harness's own state shows;
- a sub-agent is working for it;
- a tmux client is attached.

A recorded `running` is a claim, not evidence. Before any teardown the reaper
asks the harness's own state (codex `thread/read`, antigravity's cascade
status, claude's status file, opencode's permissions and bridge, devin's hook
log) and the server's pending prompts. A stale claim is refuted and the pane is
reaped. When the harness's state cannot answer, the claim gets one more window;
a harness with no such state is judged by how long the claim has gone silent.

Otherwise a pane is reaped one idle window after its last evidence of work.

## Runner idle shutdown

The runner exits after `runner.idle_timeout_s` with no work in flight, and
that tears down every native pane at once. Native delivery returns as soon as
the prompt is typed, so the runner's own turn ends while the agent works on. A
native session keeps the runner up while either holds:

- **A human wait**, whatever the recorded status says: an open prompt a
  harness mirror surfaced (a prompt park), or a dialog the agent reported
  (`blocked_on`). Each holds for at most `OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S`
  from when it opened. Runner ASKs hold the runner on their own.
- **A recorded turn with recent evidence of work.** The session's status is
  `running`, or `waiting` (the runner's turn ended while sub-agents still
  worked; a human wait is `running` with `blocked_on`, never `waiting`), on
  any channel, relays included. It holds while the turn showed first-hand
  evidence within the ceiling: the episode's start, the session's last runner
  dispatch, or output on the session's own agent pane. Client repaints and
  output on the session's other panes do not count. A relay re-posting
  `running` does not renew the hold; a new episode (`idle`, then `running`) or
  a new dispatch restarts it.

This is the one hold that trusts a recorded status (see "Session status and
liveness" in the root `AGENTS.md`), so it is bounded. The ceiling is
`OMNIGENT_NATIVE_PANE_MAX_TURN_S`, floored at 3600 s so that `0` or a small
value cannot switch the hold off (`runner_in_flight_hold_ceiling_clamped` is
logged at startup when the floor applies). A turn whose pane keeps printing is
held for as long as it prints; a silent turn is held until the ceiling after
its last evidence. When a hold expires, one WARNING per episode,
`runner_in_flight_hold_expired`, names the session, the status, the channel
that opened the episode and the evidence age.

Most holds end long before the ceiling. An idle edge on any channel, a reap
or the agent pane's exit or close (each resets the session's status), a
refutation by the harness's own state or the server, a required terminal's
exit, and deleting the session each end it at once. The ceiling matters only
for a silent claim the reaper never ends: a pane with a client attached, an
exempt harness (kimi), a role-less pane, reaping disabled, or `veto`/`shadow`
with nothing to refute the claim.

**Losing a codex or opencode TUI.** These harnesses run the turn in their
vendor server (codex app-server, `opencode serve`), not in the TUI. When the
TUI exits or is deleted mid-turn while that server is still registered, the
recorded status is kept, so the runner stays up for the turn within the
ceiling (the lost TUI's output no longer renews it). The turn's relayed `idle`
ends the hold as usual. A codex TUI re-created while its app-server still runs
attaches to that app-server, so the turn and its status carry over to the new
pane. Otherwise releasing the vendor server resets the
status: right after the DELETE when nothing needs the sidecars, by the
orphaned-sidecar sweep once they have been unneeded for an idle window, or
when the session is deleted. A required terminal's exit always resets it.
After a `/clear` rotation moved the TUI to a new session, the server is still
the launching session's: losing the TUI keeps the new session's status while
that server lives (a server of the new session's own does not count, and
sweeping it leaves that status alone), and releasing the launching session's
sidecars, or deleting that session, resets it. The sweep asks the launching
session's server about the new session's thread, so it keeps that server while
the turn is active. If the new session gets a pane of its own again, or a new
turn is sent to it, that pane or turn owns its status from then on.

## Knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S` | `3600` | Idle window. `0` disables pane reaping. |
| `OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S` | `86400` | Longest a human wait may hold a pane or the runner. |
| `OMNIGENT_NATIVE_PANE_MAX_TURN_S` | `86400` | Longest a probe's ACTIVE or running sub-agents may hold a silent pane. Also the runner's ceiling after a recorded turn's last evidence of work, floored at `3600`. |
| `OMNIGENT_NATIVE_PANE_CLAIM_POLICY` | `evidence` | `veto` or `shadow` make a recorded `running` spare the pane outright again (`shadow` also logs when it would have expired). Operator rollback only. |
| `OMNIGENT_NATIVE_PANE_REAP_SERVER_CHECK` | on | `0` skips asking the server for pending prompts before a reap. |
| `OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S` | idle window | How long an unreachable server blocks reaps before local signals decide. |

A hold found only by the pre-reap check (a prompt a probe reports, a server
prompt) counts its bound from when the wait began, and can run up to one idle
window past it. Invalid values log a warning and fall back to the default.
Non-finite values (`nan`, `inf`) fall back to the default with a warning too,
for every seconds knob here; set `OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S=0`, not
`inf`, to disable pane reaping.

## Events

All are `debug_event` records with `session_id`.

- `native_pane_spared`: the reasons holding a pane changed (`stage` is `scan`,
  `confirm` or `teardown`).
- `native_pane_reaped`: a pane or runtime was torn down, with the reasons that
  held it while the reaper watched it. Not logged when the teardown spared it.
- `native_pane_teardown`: what a reap, DELETE or sweep released.
- `native_pane_claim_refuted`: a stale `running` was refuted, and by what.
  `native_pane_claim_moved`: a refutation was skipped because a new turn's edge
  landed while it was checked.
- `native_pane_status_contradiction`, `native_pane_reap_unverified_claim`,
  `native_pane_reap_unverified`, `native_pane_hold_expired`,
  `native_pane_warning` (long holds, forgotten attach, dead watcher, idle
  repaint): WARNINGs worth a look.
- `native_pane_skipped`: a pane the listing leaves out, once per pane.
- `native_pane_reaper_summary`: counts by reason, hourly.
- `runner_in_flight_hold_expired` (WARNING): a recorded native turn stopped
  holding the runner at the ceiling, once per episode, with `status`,
  `claim_source`, `evidence_age_s` and `ceiling_s`.
  `runner_in_flight_hold_ceiling_clamped` (WARNING, at startup, no
  `session_id`): `OMNIGENT_NATIVE_PANE_MAX_TURN_S` is below the runner's floor.
- `session_status_edge` (DEBUG): every status edge the runner recorded, by
  channel. `session_status_wire_edge`: whether the watcher's edge reached the
  server or was deduplicated.

## Known limits

- An attached tmux client holds a pane indefinitely (a WARNING is logged once
  it has been the only reason for an idle window).
- After a lost idle, a native session the reaper never judges keeps the runner
  up until the ceiling (see "Runner idle shutdown").
- A prompt a kiro or qwen mirror surfaced is not seen again after a runner
  restart, because the mirror starts reading at the end of its event file.
- Harnesses without a turn probe (pi, cursor, goose, kiro, qwen, hermes) are
  judged by pane output: a TUI that prints nothing during a long internal tool
  call looks idle. Relayed tool calls are covered by their MCP lease.
- `opencode serve` is closed on teardown; MCP children that it does not stop
  itself are not killed as a process group.

## Verification

```sh
uv run --no-sync pytest -q tests/terminals tests/runner/test_native_pane_*.py \
  tests/runner/test_session_status_book.py tests/runner/test_session_status_single_recorder.py \
  tests/dev/lint/test_lint_session_status_single_source.py
```

Changing how status is recorded or read? Follow "Session status and liveness"
in the root `AGENTS.md`.
