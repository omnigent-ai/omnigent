"""The quiescence badge must not feed sub-agent terminal delivery.

A >5s transcript gap in a claude-native Task sub-agent posted status "idle",
which the runner consumed as an authoritative completion: a false "sub-agent
finished" inbox notice ~1-2 min after spawn, and the real completion was
discarded once ``delivered`` latched. The badge now posts a distinct
"quiesced" status that the server publishes for the UI but never forwards to
the runner.
"""

from __future__ import annotations

from omnigent.server.routes._sessions.common import _EXTERNAL_SESSION_STATUS_VALUES


def test_quiesced_is_an_accepted_status_value() -> None:
    assert "quiesced" in _EXTERNAL_SESSION_STATUS_VALUES


def test_forwarder_quiescence_posts_quiesced_not_idle() -> None:
    """The forwarder's quiescence branch emits the badge value, never idle."""
    import inspect

    from omnigent import claude_native_forwarder as fwd

    src = inspect.getsource(fwd)
    # The quiescence branch sets desired_status = "quiesced"; asserting on the
    # source keeps the regression tied to the branch itself (any revert to
    # "idle" reintroduces the false-terminal path).
    quiescence_block = src[
        src.index("Quiescence-based status") : src.index("Quiescence-based status") + 1400
    ]
    assert 'desired_status = "quiesced"' in quiescence_block
    assert 'desired_status = "idle"' not in quiescence_block


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
