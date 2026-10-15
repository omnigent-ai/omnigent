# Network resilience

What a user should see when the network between Omnigent's pieces breaks, and
which scenarios currently meet that bar. Every row comes from a script in
`tests/e2e/resilience/scenarios/` that runs against the
[resilience lab](../tests/e2e/resilience/README.md). The lab is a real server,
host daemon, runner and harness (the real Claude Code and Codex CLIs against
a scripted mock model) with each network link behind a fault proxy. Every
scenario runs against both claude-native and codex-native. Codex sessions are
launched in "Ask for approval" mode, so escalated commands reach the user
instead of Codex's automatic reviewer.

## Contract

| Outage | The user sees | Never |
| --- | --- | --- |
| Blip under ~5 s | Nothing, or a brief "Reconnecting…" | A failed turn, an approval denied or lost, a lost or duplicated action, a wrong status |
| Within the reconnect grace (`RUNNER_DISCONNECT_GRACE_S`, 90 s) | "Reconnecting…", and the turn still shown running when it is | A red error over a turn that is still running on the host |
| Past the grace | "Host offline since …", then the true state and one action that fixes it once reachable | A spinner forever, a status that disagrees with the harness, an error with no action |
| The harness loses its model | A visible retrying state, then success or a retryable error | A silent stall, or a terminal error without Retry |
| The user acts during an outage | The action applied exactly once when reachable, or refused at once with the input kept | Silently dropped or applied twice |

The committed transcript, session status, approvals and the effect of each
user action must end up the same as in an uninterrupted run. Live preview text
during an outage is best-effort.

## Scenarios

| ID | Real-world cause | Fault in the lab | Script |
| --- | --- | --- | --- |
| S1 | Ingress connection recycling, front-door request cap | Browser links recycled every 8 s, host links after the grace, 504 for requests held 15 s | [`test_s1_ingress_recycle.py`](../tests/e2e/resilience/scenarios/test_s1_ingress_recycle.py) |
| S2 | Server deploy or restart | Server stopped (SIGTERM), 502 from the front | [`test_s2_server_restart.py`](../tests/e2e/resilience/scenarios/test_s2_server_restart.py) |
| S3 | Host network change (Wi-Fi, VPN, marginal link) | Host and model links half-open (`blackhole`), reset and refused (`reset`), or dropped 2 s every 15 s (`flap`) | [`test_s3_host_network_change.py`](../tests/e2e/resilience/scenarios/test_s3_host_network_change.py) |
| S4 | Host sleep | Host processes SIGSTOPped and host links blackholed; network returns 2 s after thaw | [`test_s4_host_sleep.py`](../tests/e2e/resilience/scenarios/test_s4_host_sleep.py) |
| S5 | Host offline while the user is elsewhere | Host and model links refused; the user sends, approves or stops | [`test_s5_host_offline_user_acts.py`](../tests/e2e/resilience/scenarios/test_s5_host_offline_user_acts.py) |
| S6 | Client offline | Browser link half-open or refused while a real page is open | [`test_s6_client_offline.py`](../tests/e2e/resilience/scenarios/test_s6_client_offline.py) |
| S7 | Model or gateway outage | Model link refused, before the first model call or mid-stream | [`test_s7_model_loss.py`](../tests/e2e/resilience/scenarios/test_s7_model_loss.py) |
| S8 | Credentials expire during the outage | Not yet: needs an authenticated lab mode | — |

## Matrix

A **pass** row held every check. A **gap** row fails a check today. Each gap
is pinned as a strict expected failure (`xfail(strict=True)`, or `known_gap` on
the single check), so a fix that makes it pass must also remove the marker.
The one exception is R8: it is a race, so its marker does not turn stale when
a run happens to pass. Outages beyond the short default run only with
`OMNIGENT_E2E_RESILIENCE_FULL=1`.

| Scenario | Phase / action | Outage | claude-native | codex-native |
| --- | --- | --- | --- | --- |
| S1 | tool running, approval pending | 40 s / 180 s window | pass | pass |
| S2 | idle; tool running; tool ends during outage | 5 s / 60 s / 120 s | pass | pass |
| S2 | approval pending | 5 s | pass | pass |
| S2 | approval pending | 60 s | gap: [R1](#r1-approval-card-missing-after-the-link-returns) | gap: [R1](#r1-approval-card-missing-after-the-link-returns) |
| S2 | approval pending | 120 s | gap: [R2](#r2-long-outage-moves-the-approval-to-the-terminal) | gap: [R1](#r1-approval-card-missing-after-the-link-returns) |
| S3 blackhole | idle, tool running, approval pending | 10 s / 45 s / 120 s | pass | pass, [R8](#r8-codex-status-sticks-on-running-after-a-reconnect) intermittent |
| S3 reset | idle, tool running | 10 s / 45 s / 120 s | pass | pass, R8 intermittent |
| S3 reset | approval pending | 10 s | pass | pass, R8 intermittent |
| S3 reset | approval pending | 45 s / 120 s | gap: R1 / R2 | pass, R8 intermittent |
| S3 flap (full mode only) | idle; tool running, approval pending | 40 s | pass | pass, R8 intermittent |
| S3 flap (full mode only) | idle | 120 s | pass | pass, R8 intermittent |
| S3 flap (full mode only) | tool running, approval pending | 120 s | gap: [R6](#r6-repeated-blips-add-up-to-a-disconnect-failure) | gap: R6 |
| S4 | idle, tool running, approval pending | 10 s / 60 s / 300 s | pass | pass |
| S5 | approve | 20 s | pass | pass |
| S5 | approve | 150 s | gap: R2 | pass |
| S5 | send | 20 s / 150 s | gap: [R3](#r3-a-message-sent-while-the-host-is-unreachable-is-lost) | gap: R3 |
| S5 | stop | 20 s / 150 s | pass | pass |
| S6 | tool ends during outage, approval pending (half-open and refused) | 20 s / 120 s | gap: [R5](#r5-the-page-never-says-it-is-offline) | gap: R5 |
| S7 | before the first call, mid-stream | 10 s / 60 s | pass | pass |
| S7 | before the first call | 180 s | pass | pass |
| S7 | mid-stream | 180 s | gap: [R7](#r7-a-turn-that-lost-the-model-has-no-retry) | pass |

## Findings

### R1: Approval card missing after the link returns

Both harnesses re-POST a held approval with backoff capped at 30 s. Claude
does this in `_post_hook_with_reattach` in
`omnigent/harnesses/claude_native/hook.py`. Codex does it in
`_post_codex_elicitation_request` in
`omnigent/harnesses/codex_native/forwarder.py`. The server keeps pending
approvals in memory, so a restart or a refused host link loses the card until
the harness's next attempt, which can be up to 30 s after the link is back.
In the lab the card returned 28–30 s after the server did. An approval sent
from the stale card in that gap was accepted with `202` but had no effect, and
the turn stayed blocked until the user answered the returned card. Codex keeps
retrying for up to a day, so for Codex this is also the 120 s outcome.

### R2: Long outage moves the approval to the terminal

Claude only. After `OMNIGENT_HOOK_MAX_RETRIES` (8) consecutive failed re-POSTs, about 90 s
of backoff, the hook gives up and Claude Code falls back to its own terminal
prompt. When the link returns, the web never shows the card again, and an
approval the user already gave while the host was offline never reaches
Claude. The session stays `running`, and new messages queue behind a prompt
that only the terminal can answer.

### R3: A message sent while the host is unreachable is lost

Both harnesses; this is server behavior. With the host's links down for 20 s, a message sent from the browser returned
`202` after about 10 s. The server then tried to relaunch the runner through
the unreachable host and published `failed` with `runner_failed_to_start`. The
message never reached the runner. The failure stayed after the host
reconnected, because passive recovery clears only `runner_disconnected`.

### R4: Stop reports success while the host is unreachable

**Resolved.** Stop returns `503` promptly when the host is unreachable and
runner termination cannot be confirmed. The user can retry after reconnection.
The refused Stop leaves the existing turn intact, so it finishes when the
host returns.

Verified with Claude and Codex during both 20 s and 150 s refused-link
outages. All four cases preserve exactly one user message, final reply and
tool result, settle to idle, and accept the next turn after reconnection.
The 20 s cases also show no failed status within the reconnect grace.

### R5: The page never says it is offline

With the browser's link half-open or refused for 20 s, nothing on the page
indicated a lost connection. It kept showing the last state it had received,
for example "Blocked on: permission prompt" for an approval that had already
been answered. The page does catch up without a reload once the link returns:
it shows the reply once, the approval card can still be answered, and the next
message works.

### R6: Repeated blips add up to a disconnect failure

Both harnesses; this is server behavior. With the host link dropping for 2 s every 15 s, each drop reconnected within
seconds. About 100 s after the first drop the session published `failed`
(`runner_disconnected`) over the running turn, then recovered 4 s later. The
relay supervisor (`_relay_runner_stream` in
`omnigent/server/routes/_sessions/orchestration.py`) starts a fresh grace
window only after an attempt that streamed longer than the grace. Shorter
healthy stretches keep counting against the first drop's 90 s deadline.

### R7: A turn that lost the model has no Retry

Claude only. When Claude Code exhausted its retries against an unreachable model (about
150 s), it ended the turn with "API Error: Connection refused — a firewall or
proxy may be blocking it (ECONNREFUSED)". `classify_native_turn_error` labels
that `native_turn_error`, which the web UI does not offer Retry for. Shorter
model outages of up to 60 s were retried and completed. Claude's retrying is
not surfaced anywhere in the session while it happens.

### R8: Codex status sticks on running after a reconnect

Codex only, and intermittent. After the host's links drop and return, the next
turn's status edges arrive as `running`, `idle`, `running`, and the last one
sticks. The session shows "working" indefinitely, even though the reply was
committed. In the lab logs the Codex forwarder posts exactly one `running` and
one `idle`. The extra `running` lands just as the server's relay reconnects to
the runner's session stream. The likely cause is that the relay receives
events the runner buffered during the outage: the stream has no cursor, so
they replay late and out of order. The race hit 4 of 30 short S3 Codex runs,
plus the blackhole and flap idle rows at 120 s.

### Other observations

- Each Codex tool call made while the server is down waits on the
  evaluate-policy hook's 30 s retry budget before proceeding. A chained tool
  turn therefore slows down during an outage, but it completes.
- In "Approve for me" (the default Codex mode), escalated commands go to
  Codex's automatic reviewer, which makes its own model call. When that call
  returned an unusable verdict, Codex rejected the command rather than asking
  the user. A model outage at that moment would likely do the same. This is
  not yet a scenario row.

## Running and reading results

Set `OMNIGENT_RESILIENCE_VIDEO=both` to record the user's view of every run,
with each fault captioned on screen
([how](../tests/e2e/resilience/README.md#recording-what-the-user-sees)).

```sh
uv run --no-sync pytest tests/e2e/resilience/scenarios -v              # short outages
OMNIGENT_E2E_RESILIENCE_FULL=1 uv run --no-sync pytest tests/e2e/resilience/scenarios -n 2
uv run --no-sync python -m tests.e2e.resilience.lab.report              # matrix of saved runs
```

Each run writes a JSON and Markdown report with every check and the session's
status timeline to `.omnigent/resilience/`. Set
`OMNIGENT_RESILIENCE_REPORT_DIR` to write them elsewhere. Set
`OMNIGENT_RESILIENCE_KEEP=1` to keep passing lab roots as well as failing ones.
