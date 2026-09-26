"""Tests for opencode-native runner orchestration helpers (launch, fork, TUI args)."""

from __future__ import annotations

from typing import Any

import omnigent.runner.native.orchestration as orchestration


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _SnapshotClient:
    """Async client stub returning one session snapshot."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def get(
        self, url: str, timeout: float | None = None, params: dict[str, str] | None = None
    ) -> _Resp:
        return _Resp(200, self._snapshot)


async def test_launch_config_reads_fork_source_labels(monkeypatch: Any) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "labels": {
            "omnigent.fork.source_id": "conv_source",
            "omnigent.fork.source_external_session_id": "ses_src",
            "omnigent.fork.carry_history": "1",
        },
    }
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_clone",
        server_client=_SnapshotClient(snapshot),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id == "conv_source"
    assert cfg.fork_source_external_id == "ses_src"
    assert cfg.fork_carry_history is True


async def test_launch_config_without_fork_labels(monkeypatch: Any) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_plain",
        server_client=_SnapshotClient({"workspace": "/tmp/repo"}),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id is None
    assert cfg.fork_source_external_id is None
    assert cfg.fork_carry_history is False
