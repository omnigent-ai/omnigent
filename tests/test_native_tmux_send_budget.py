"""The per-command tmux send budget must be uniform across native bridges.

Every native harness bridge shells out to ``tmux`` for keystroke delivery
with a flat per-command subprocess timeout. A bridge whose budget is shorter
than its siblings' dies first under the same machine load: parallel worker
boots on a large worktree can starve a healthy tmux server past 5s, and the
first dispatch send then fails the worker on infra (or is silently lost,
stranding the supervisor). The identical operation must get the identical
budget everywhere, and that budget must cover a realistic boot-load stall.
"""

from __future__ import annotations

import pytest

from omnigent import (
    antigravity_native_bridge,
    claude_native_bridge,
    cursor_native_bridge,
    goose_native_bridge,
    hermes_native_bridge,
    kimi_native_bridge,
    kiro_native_bridge,
    qwen_native_bridge,
)

_BRIDGES = {
    "antigravity": antigravity_native_bridge,
    "claude": claude_native_bridge,
    "cursor": cursor_native_bridge,
    "goose": goose_native_bridge,
    "hermes": hermes_native_bridge,
    "kimi": kimi_native_bridge,
    "kiro": kiro_native_bridge,
    "qwen": qwen_native_bridge,
}

# The reference budget: what a bridge must give one tmux command. 10s rides
# out a several-second server stall from parallel boots while still failing
# fast on a genuinely dead server.
_REFERENCE_BUDGET_S = cursor_native_bridge._TMUX_SEND_TIMEOUT_S


@pytest.mark.parametrize("name", sorted(_BRIDGES))
def test_tmux_send_budget_is_uniform_across_bridges(name: str) -> None:
    """Every bridge gives a tmux command the same send budget.

    A shorter budget on one bridge means that harness's workers die on the
    same load its siblings survive — the asymmetry that killed claude-native
    workers at 5.0s while cursor-native rode out the identical stall at 10s.
    """
    budget = _BRIDGES[name]._TMUX_SEND_TIMEOUT_S
    assert budget == _REFERENCE_BUDGET_S, (
        f"{name}_native_bridge gives a tmux command {budget}s while its "
        f"siblings give {_REFERENCE_BUDGET_S}s — under the same server stall "
        "this bridge's workers fail first"
    )


def test_tmux_send_budget_covers_a_boot_load_stall() -> None:
    """The shared budget rides out a realistic parallel-boot server stall.

    Parallel native-worker boots on a large worktree have been observed to
    starve a healthy tmux server for ~7s; a budget at or below that kills the
    first dispatch send of every worker that boots under load.
    """
    assert _REFERENCE_BUDGET_S >= 10.0
