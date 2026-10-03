# Network resilience

What a user should see when the network between Omnigent's pieces breaks, and
which scenarios currently meet that bar. Every row comes from a script in
`tests/e2e/resilience/scenarios/` that runs against the
[resilience lab](../tests/e2e/resilience/README.md). The lab is a real server,
host daemon, runner and harness with each network link behind a fault proxy.

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

## Matrix: claude-native

A **pass** row held every check. A **gap** row fails a check today. Each gap
is pinned as a strict expected failure (`xfail(strict=True)`, or `known_gap` on
the single check), so a fix that makes it pass must also remove the marker.
Outages beyond the short default run only with `OMNIGENT_E2E_RESILIENCE_FULL=1`.

| Scenario | Phase / action | Outage | Result |
| --- | --- | --- | --- |
| S1 | tool running, approval pending | 40 s / 180 s window | pass |
| S2 | idle; tool running; tool ends during outage | 5 s / 60 s / 120 s | pass |
| S2 | approval pending | 5 s | pass |
| S2 | approval pending | 60 s | gap: [R1](#r1-approval-card-missing-after-the-server-returns) |
| S2 | approval pending | 120 s | gap: [R2](#r2-long-outage-moves-the-approval-to-the-terminal) |
| S3 blackhole | idle, tool running, approval pending | 10 s / 45 s / 120 s | pass |
| S3 reset | idle, tool running | 10 s / 45 s / 120 s | pass |
| S3 reset | approval pending | 10 s | pass |
| S3 reset | approval pending | 45 s / 120 s | gap: [R1](#r1-approval-card-missing-after-the-server-returns) / [R2](#r2-long-outage-moves-the-approval-to-the-terminal) |
| S3 flap | idle | 40 s / 120 s | pass |
| S3 flap | tool running, approval pending | 40 s | pass |
| S3 flap | tool running, approval pending | 120 s | gap: [R6](#r6-repeated-blips-add-up-to-a-disconnect-failure) |
| S4 | idle, tool running, approval pending | 10 s / 60 s / 300 s | pass |
| S5 | approve | 20 s | pass |
| S5 | approve | 150 s | gap: [R2](#r2-long-outage-moves-the-approval-to-the-terminal) |
| S5 | send | 20 s / 150 s | gap: [R3](#r3-a-message-sent-while-the-host-is-unreachable-is-lost) |
| S5 | stop | 20 s / 150 s | gap: [R4](#r4-stop-reports-success-while-the-host-is-unreachable) |
| S6 | tool ends during outage, approval pending (half-open and refused) | 20 s | gap: [R5](#r5-the-page-never-says-it-is-offline) (catches up, shows the reply once, and the next message works) |
| S7 | before the first call, mid-stream | 10 s / 60 s | pass (Claude retries) |
| S7 | before the first call | 180 s | pass |
| S7 | mid-stream | 180 s | gap: [R7](#r7-a-turn-that-lost-the-model-has-no-retry) |

## Findings

### R1: Approval card missing after the server returns

The Claude permission hook re-POSTs its held approval with exponential backoff
capped at 30 s (`_post_hook_with_reattach` in
`omnigent/harnesses/claude_native/hook.py`). The server keeps pending approvals
in memory, so a restart or a refused host link loses the card until the hook's
next attempt. That can be up to 30 s after the link is back. In the lab, the
card returned 30 s after the server did. An approval sent from the stale card
in that gap was accepted with `202` but had no effect, and the turn stayed
blocked.

### R2: Long outage moves the approval to the terminal

After `OMNIGENT_HOOK_MAX_RETRIES` (8) consecutive failed re-POSTs, about 90 s
of backoff, the hook gives up and Claude Code falls back to its own terminal
prompt. When the link returns, the web never shows the card again, and an
approval the user already gave while the host was offline never reaches
Claude. The session stays `running`, and new messages queue behind a prompt
that only the terminal can answer.

### R3: A message sent while the host is unreachable is lost

With the host's links down for 20 s, a message sent from the browser returned
`202` after about 10 s. The server then tried to relaunch the runner through
the unreachable host and published `failed` with `runner_failed_to_start`. The
message never reached the runner. The failure stayed after the host
reconnected, because passive recovery clears only `runner_disconnected`.

### R4: Stop reports success while the host is unreachable

With no live tunnel the server finds no runner to stop. It treats the Stop as
done and shows the session idle. The turn keeps running on the host and
finishes after the host returns. The next message is delivered while Claude
is still busy with the "stopped" turn, and it never appears in Claude's
transcript.

### R5: The page never says it is offline

With the browser's link half-open or refused for 20 s, nothing on the page
indicated a lost connection. It kept showing the last state it had received,
for example "Blocked on: permission prompt" for an approval that had already
been answered. The page does catch up without a reload once the link returns:
it shows the reply once, the approval card can still be answered, and the next
message works.

### R6: Repeated blips add up to a disconnect failure

With the host link dropping for 2 s every 15 s, each drop reconnected within
seconds. About 100 s after the first drop the session published `failed`
(`runner_disconnected`) over the running turn, then recovered 4 s later. The
relay supervisor (`_relay_runner_stream` in
`omnigent/server/routes/_sessions/orchestration.py`) starts a fresh grace
window only after an attempt that streamed longer than the grace. Shorter
healthy stretches keep counting against the first drop's 90 s deadline.

### R7: A turn that lost the model has no Retry

When Claude Code exhausted its retries against an unreachable model (about
150 s), it ended the turn with "API Error: Connection refused — a firewall or
proxy may be blocking it (ECONNREFUSED)". `classify_native_turn_error` labels
that `native_turn_error`, which the web UI does not offer Retry for. Shorter
model outages of up to 60 s were retried and completed. Claude's retrying is
not surfaced anywhere in the session while it happens.

## Running and reading results

```sh
uv run --no-sync pytest tests/e2e/resilience/scenarios -v              # short outages
OMNIGENT_E2E_RESILIENCE_FULL=1 uv run --no-sync pytest tests/e2e/resilience/scenarios -n 2
uv run --no-sync python -m tests.e2e.resilience.lab.report              # matrix of saved runs
```

Each run writes a JSON and Markdown report with every check and the session's
status timeline to `.omnigent/resilience/`. Set
`OMNIGENT_RESILIENCE_REPORT_DIR` to write them elsewhere. Set
`OMNIGENT_RESILIENCE_KEEP=1` to keep passing lab roots as well as failing ones.
