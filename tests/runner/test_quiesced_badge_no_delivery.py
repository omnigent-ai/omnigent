"""Older runners' quiesced status stays accepted without terminal delivery.

New runners send subagent.status with idle=true. Keep the legacy input
compatible until its planned removal in 0.16.0.
"""

from __future__ import annotations

from omnigent.server.routes._sessions.common import _EXTERNAL_SESSION_STATUS_VALUES


def test_quiesced_is_an_accepted_status_value() -> None:
    assert "quiesced" in _EXTERNAL_SESSION_STATUS_VALUES


def test_runner_terminal_branch_ignores_quiesced() -> None:
    """The runner's terminal-delivery condition admits only idle/failed."""
    import inspect

    from omnigent.runner import app as runner_app

    src = inspect.getsource(runner_app)
    terminal_block = src[
        src.index('if status in ("idle", "failed"):') : src.index(
            'if status in ("idle", "failed"):'
        )
        + 400
    ]
    assert 'status == "idle"' in terminal_block
    assert '"quiesced"' not in terminal_block, (
        "the badge value must never appear in the terminal path"
    )
