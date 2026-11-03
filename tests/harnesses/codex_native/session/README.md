# Codex session orchestration tests

Existing scenarios from `test_codex_native.py`, grouped by behavior. Test names,
assertions and parameter cases are retained. Counts below are test definitions;
parameterization produces additional collected cases.

| Module | Behavior | Test definitions |
| --- | --- | ---: |
| [test_approvals.py](test_approvals.py) | Approvals | 7 |
| [test_auth.py](test_auth.py) | Auth | 22 |
| [test_compaction.py](test_compaction.py) | Compaction | 11 |
| [test_deltas.py](test_deltas.py) | Deltas | 12 |
| [test_elicitation.py](test_elicitation.py) | Elicitation | 8 |
| [test_launch_config.py](test_launch_config.py) | Launch config | 18 |
| [test_local_sessions.py](test_local_sessions.py) | Local sessions | 11 |
| [test_plan_implementation.py](test_plan_implementation.py) | Plan implementation | 4 |
| [test_resume_permissions.py](test_resume_permissions.py) | Resume permissions | 13 |
| [test_resume_rollouts.py](test_resume_rollouts.py) | Resume rollouts | 18 |
| [test_session_rotation.py](test_session_rotation.py) | Session rotation | 4 |
| [test_subagents.py](test_subagents.py) | Subagents | 10 |
| [test_subscription.py](test_subscription.py) | Subscription | 17 |
| [test_terminal_attach.py](test_terminal_attach.py) | Terminal attach | 9 |
| [test_terminal_prepare.py](test_terminal_prepare.py) | Terminal prepare | 13 |
| [test_tools.py](test_tools.py) | Tools | 19 |
| [test_transcript_delivery.py](test_transcript_delivery.py) | Transcript delivery | 8 |
| [test_transport.py](test_transport.py) | Transport | 5 |
| [test_usage.py](test_usage.py) | Usage | 11 |

The autouse catalog stub is confined to this directory in `conftest.py`.
Helpers shared by multiple modules live in `_support.py`; helpers used by one
module stay beside their tests. Search a retained test name to find an old failure:

```sh
rg 'def test_name' tests/harnesses/codex_native/session
uv run --no-sync pytest tests/harnesses/codex_native/session --reruns 0 -n 4 --dist loadfile
```
