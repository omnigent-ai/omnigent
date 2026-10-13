# IBM Bob Shell native harness (`bob-native`)

`omnigent bob` runs IBM Bob Shell's interactive TUI (`bob chat`) in a
runner-owned tmux terminal, attaches your terminal to it, and embeds the same
pane in the web UI. Messages sent from the web composer are pasted into Bob's
input box. Bob's replies appear in its terminal; they are not mirrored into the
chat transcript.

## Requirements

- Bob Shell **2.0.0 or newer** (`bob --version`). 1.0.x is a different CLI
  without `bob chat`. Install or upgrade with IBM's checksum-verifying script:
  `curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash`.
- `tmux`. macOS and Linux only; native Windows is not supported for native
  TUI harnesses.

To use a `bob` that is not first on `PATH`, set `OMNIGENT_BOB_PATH` (or
`harness.bob-native.command` in the Omnigent config); it reaches the runner.

Verified against Bob Shell 2.0.5.

## Sign-in, license, and folder trust

Bob owns these. On first launch Bob shows its own prompts in its terminal:
IBMid sign-in in the browser, the license agreement, and "Do you trust this
folder?". Bob stores the sign-in under `~/.bob`, and later launches reuse it.
Omnigent stores no Bob credential, never adds `--trust`, `--accept-license`, or
`--auto-approve`, and never presses keys in Bob's dialogs.

API-key auth (`BOB_API_KEY`, which IBM documents for `bob run`/CI) works when
you run `bob` yourself. Omnigent deliberately does not forward it: Bob's
terminal starts from an allowlisted environment (`PATH`, `HOME`, locale and
terminal variables, `HTTP(S)_PROXY`/`NO_PROXY`, `NODE_EXTRA_CA_CERTS`,
`BOB_LOG_LEVEL`) instead of the runner's, so no credential sits in the
long-lived pane. Sign in once with IBMid instead.

Bob's dialogs are selection lists where Enter accepts the highlighted choice
("Trust folder", "Approve Once"). While one is on screen, Omnigent refuses to
deliver web messages or send Stop's Escape, and the web turn fails with
"Bob is waiting on a prompt in its terminal". Answer the prompt in the
terminal, then send again.

## Launch options

Pass documented `bob chat` options after `--`:

```sh
omnigent bob -- --mode plan
omnigent bob -- --resume <bob-task-id>
omnigent bob -- --trust          # your explicit answer to the trust prompt
```

Accepted: `--mode`, `--resume`/`-r`, `--instance-id`, `--team-id`,
`--log-level`, `--max-cost`, `--max-turns`, `--disable-mcp`,
`--disable-subagents`, `--disable-tool-groups`, `--trust`, `--accept-license`.
Rejected: `--auto-approve` (Bob's approval dialog is the only tool gate here;
toggle auto-approve inside Bob if you want it), `--workspace` (Omnigent uses
the session directory), and `--model` (Bob 2.x has no model flag).

## What works

| Capability | bob-native |
|---|---|
| Launch from web picker and `omnigent bob` | yes |
| Web composer → Bob input (incl. mid-turn steering) | yes, when the composer is visible |
| Stop (interrupt) | Escape, Bob's documented interrupt key |
| Stop session | kills Bob's tmux session |
| `omnigent bob --resume <conv>` | reattaches a running Bob terminal; if it exited, starts a fresh `bob chat` |
| Model / effort picker | no (choose the model inside Bob); a persisted model or effort override is refused at launch |
| Omnigent approval cards, policies, hooks | no (Bob's own approvals apply) |
| Chat transcript mirroring, fork history, Omnigent sub-agents | no |

## Limitations

- An unsent draft typed directly in Bob's terminal is kept, so a web message
  is appended to it. Bob's clear key (Ctrl+C) would also interrupt a running
  turn, so Omnigent does not clear drafts.
- Environment variables outside the allowlist above do not reach Bob.
- Omnigent-level resume does not map an Omnigent session to a Bob task: Bob
  2.0.5's `bob --list-tasks` fails when stdout is not a TTY. Pass
  `-- --resume <task-id>` yourself to continue a Bob task.
- Bob 2.0.2+ also offers `bob acp`; it is not used because it does not give
  you Bob's own TUI.
