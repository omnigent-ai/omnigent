"""Prepared workspaces report progress through the snapshot and live stream."""

from unittest.mock import Mock

import pytest

from omnigent.server.routes._sessions import helpers
from omnigent.server.routes._sessions.common import _session_sandbox_status_cache


@pytest.mark.parametrize("stage", ["cloning", "preparing_workspace"])
def test_workspace_progress_publishes_and_clears(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    session_id = "workspace-progress-test"
    publish = Mock()
    monkeypatch.setattr(helpers.session_stream, "publish", publish)
    try:
        helpers._publish_sandbox_status_impl(session_id, stage)
        assert _session_sandbox_status_cache[session_id].stage == stage
        publish.assert_called_once_with(
            session_id,
            {
                "sequence_number": None,
                "type": "session.sandbox_status",
                "conversation_id": session_id,
                "stage": stage,
                "error": None,
            },
        )

        helpers._publish_sandbox_status_impl(session_id, "ready")
        assert session_id not in _session_sandbox_status_cache
        assert publish.call_args.args[1]["stage"] == "ready"
    finally:
        _session_sandbox_status_cache.pop(session_id, None)
