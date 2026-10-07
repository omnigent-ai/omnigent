# Codex forwarding tests

Existing scenarios from `test_codex_native_forwarder.py`, grouped by behavior. Test names,
assertions and parameter cases are retained. Counts below are test definitions;
parameterization produces additional collected cases.

| Module | Behavior | Test definitions |
| --- | --- | ---: |
| [test_compaction.py](test_compaction.py) | Compaction | 8 |
| [test_delivery.py](test_delivery.py) | Delivery | 15 |
| [test_deltas.py](test_deltas.py) | Deltas | 14 |
| [test_elicitation.py](test_elicitation.py) | Elicitation | 5 |
| [test_health.py](test_health.py) | Health | 4 |
| [test_instructions.py](test_instructions.py) | Instructions | 20 |
| [test_mcp_startup.py](test_mcp_startup.py) | Mcp startup | 14 |
| [test_replay_order.py](test_replay_order.py) | Replay order | 4 |
| [test_settings.py](test_settings.py) | Settings | 20 |
| [test_side_chat.py](test_side_chat.py) | Side chat | 1 |
| [test_subagents.py](test_subagents.py) | Subagents | 10 |
| [test_turn_errors.py](test_turn_errors.py) | Turn errors | 31 |
| [test_usage.py](test_usage.py) | Usage | 2 |
| [test_user_messages.py](test_user_messages.py) | User messages | 4 |

Helpers shared by multiple modules live in `_support.py`; helpers used by one
module stay beside their tests. Search a retained test name to find an old failure:

```sh
rg 'def test_name' tests/harnesses/codex_native/forwarder
uv run --no-sync pytest tests/harnesses/codex_native/forwarder --reruns 0 -n 4 --dist loadfile
```
