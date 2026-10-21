# Claude-native forwarder tests

These files split the former `test_claude_native_forwarder.py` by behavior.
Test names and parameter IDs are retained: locate a historical failure's test
name with `rg 'def test_name' tests/harnesses/claude_native/forwarder`.

| Behavior | Test module |
| --- | --- |
| Clear, fork, resume and native session identity | `test_session_lifecycle.py` |
| Visible and web-injected transcript items | `test_transcript_delivery.py` |
| Byte cursors, relocation, fingerprints and state writes | `test_transcript_cursors.py` |
| Pending settlement and scheduled wake turns | `test_settlement.py` |
| Parent status, stop failures and short turns | `test_status.py` |
| Compaction hook edges and spinner dismissal | `test_compaction_hooks.py` |
| Compaction persistence, recovery and duplicate suppression | `test_compaction_recovery.py` |
| Child registration, parent graphs and parked checkpoints | `test_subagent_discovery.py` |
| Child batches, retries, concurrent draining and dead letters | `test_subagent_delivery.py` |
| Child hook filtering, idle observations and parent progress | `test_subagent_status.py` |
| Auth refresh, item delivery retries and response parsing | `test_transport.py` |
| Loop supervision, deadlines and retry budgets | `test_supervision.py` |
| Missing transcripts, hook stderr and degraded sync logging | `test_diagnostics.py` |
| Model, title, permission, effort and pane signals | `test_metadata.py` |
| Task and todo snapshots | `test_todos.py` |
| Streaming deltas, batching and offsets | `test_deltas.py` |
| Token spans, cost estimates and reporting | `test_usage.py` |

`_support.py` contains the existing helpers used by multiple forwarder modules.
Helpers used by one module remain with their tests. Server shutdown, thread joins,
task cancellation and assertions stay in the scenarios that own them.

`conftest.py` applies the per-test temporary bridge root only to this directory.
Keep it here: moving it to the adapter parent would change unrelated tests.
The OpenTelemetry exporter fixture remains local to `test_usage.py`.

Run the forwarder suite with:

```sh
uv run --no-sync pytest tests/harnesses/claude_native/forwarder --reruns 0 -n 4 --dist loadfile
```

Run the surrounding adapter family by removing `/forwarder` from that path.
Both remain in the default pytest lane; file-based shard assignments and full
pytest node IDs change with the split.
