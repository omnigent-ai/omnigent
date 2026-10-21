# Codex forwarding tests

Existing scenarios from `test_codex_native_forwarder.py`, grouped by behavior. Test names,
assertions and parameter cases are retained. Counts below are test definitions;
parameterization produces additional collected cases.

| Module | Behavior | Test definitions |
| --- | --- | ---: |
| [test_compaction.py](test_compaction.py) | Compaction | 8 |
| [test_delivery.py](test_delivery.py) | Delivery | 15 |
| [test_delivery_limits.py](test_delivery_limits.py) | Upload limits and completion after oversized events | 3 |
| [test_deltas.py](test_deltas.py) | Deltas | 14 |
| [test_elicitation.py](test_elicitation.py) | Elicitation | 5 |
| [test_health.py](test_health.py) | Health | 4 |
| [test_instructions.py](test_instructions.py) | Instructions | 20 |
| [test_mcp_startup.py](test_mcp_startup.py) | Mcp startup | 14 |
| [test_replay_order.py](test_replay_order.py) | Replay order | 4 |
| [test_settings.py](test_settings.py) | Settings | 20 |
| [test_side_chat.py](test_side_chat.py) | Side chat | 1 |
| [test_subagents.py](test_subagents.py) | Subagents | 16 |
| [test_turn_errors.py](test_turn_errors.py) | Turn errors | 21 |
| [test_usage.py](test_usage.py) | Usage | 2 |
| [test_user_messages.py](test_user_messages.py) | User messages | 4 |

Helpers shared by multiple modules live in `_support.py`; helpers used by one
module stay beside their tests. Search a retained test name to find an old failure:

```sh
rg 'def test_name' tests/harnesses/codex_native/forwarder
uv run --no-sync pytest tests/harnesses/codex_native/forwarder --reruns 0 -n 4 --dist loadfile
```

The delivery-limit tests use real HTTPX encoding with a transport that models an
early body rejection as a read error. They verify that tool output is capped
before upload, other events over the 10 MiB wire limit are preserved in the
bridge's `dead_letter.jsonl`, and later completion clears the active turn and
publishes idle. They also cover UTF-8, JSON escaping, and the exact byte boundary.

The child-stream cases check live command starts and output, sibling isolation,
interrupted text, late deltas, terminal errors, completion deduplication, and
shutdown draining without changing the parent's bridge state. Child file edits
still reach the shared runner's file observer, including when siblings use the
same completed-item ID.

## Live child visibility

`tests/e2e/test_codex_native_child_visibility_e2e.py` additionally drives the real
CLI, server, host/runner, Codex app-server, model-spawned child, and Chrome. It
holds a shell command open while checking terminal ownership, command visibility,
and live stdout in the browser. It uses `isolated_local_server`; its own PTY
launch and configuration copy are needed to exercise native spawning with real
credentials while isolating the developer's active sessions.

Install the checkout's Python test dependencies and locked `web` dependencies.
Provide `codex`, `tmux`, `node`, `agent-browser`, and `google-chrome` on `PATH`,
then opt into real inference with an existing Codex configuration:

```sh
OMNIGENT_NATIVE_E2E_CODEX_HOME="$HOME/.codex" \
  .venv/bin/python -m pytest tests/e2e/test_codex_native_child_visibility_e2e.py \
  -q --reruns 0
```

Set `OMNIGENT_NATIVE_E2E_OUTPUT_DIR` to retain screenshots and session evidence.
The separate runtime directory contains credentials and must remain private.
The test skips without explicit configuration; a skip is not verification.
This covers a local CLI-launched session and its browser view, not hosted
deployment or native vendor behavior in other harnesses.
