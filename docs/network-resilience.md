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

| ID | Real-world cause | Links that break | Script |
| --- | --- | --- | --- |
| S1 | Ingress connection recycling, front-door request cap | One of client, host, runner | — |
| S2 | Server deploy or restart | Every link to the server | [`test_s2_server_restart.py`](../tests/e2e/resilience/scenarios/test_s2_server_restart.py) |
| S3 | Host network change (Wi-Fi, VPN) | Host, runner and model, possibly half-open | — |
| S4 | Host sleep | All host-side processes frozen | — |
| S5 | Host offline while the user is elsewhere | Host, runner and model | — |
| S6 | Client offline or a backgrounded mobile app | Client only | — |
| S7 | Model or gateway outage | Model only | — |
| S8 | Credentials expire during the outage | Reconnects get 401/302 | — |

## Matrix: claude-native

`pass` means every check in the script held. `gap` means the row fails today.
It is marked `xfail(strict=True)` with its finding, so a fix that makes it pass
must also remove the marker. Outages of 60 s and 120 s run only with
`OMNIGENT_E2E_RESILIENCE_FULL=1`.

| Scenario | Phase | Outage | Result |
| --- | --- | --- | --- |
| S2 | idle | 5 s / 60 s / 120 s | pass |
| S2 | tool running through the outage | 5 s / 60 s / 120 s | pass |
| S2 | tool ends during the outage | 5 s / 60 s / 120 s | pass |
| S2 | approval pending | 5 s | pass |
| S2 | approval pending | 60 s | gap: [R1](#r1-approval-card-missing-after-the-server-returns) |
| S2 | approval pending | 120 s | gap: [R2](#r2-long-outage-moves-the-approval-to-the-terminal) |

## Findings

### R1: Approval card missing after the server returns

The Claude permission hook re-POSTs its held approval with exponential backoff
capped at 30 s (`_post_hook_with_reattach` in
`omnigent/harnesses/claude_native/hook.py`). The server keeps pending approvals
in memory, so a restart loses the card until the hook's next attempt. That can
be up to 30 s after the server is back. In the lab, the card returned 30 s
after the server did, and an approval sent from the stale card in that gap was
accepted with `202` but had no effect. The turn stayed blocked.

### R2: Long outage moves the approval to the terminal

After `OMNIGENT_HOOK_MAX_RETRIES` (8) consecutive failed re-POSTs, about 90 s
of backoff, the hook gives up and Claude Code falls back to its own terminal
prompt. When the server returns, the web never shows the card again. The
session stays `running` and new messages queue behind a prompt that only the
terminal can answer. A laptop sleeping through a pending approval reaches the
same state.

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
