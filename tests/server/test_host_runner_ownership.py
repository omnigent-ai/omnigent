"""Only a host runner's owner may stop it; a sub-agent riding an ancestor's runner may not."""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions import helpers as sessions_helpers
from omnigent.server.routes._sessions.helpers import _owns_host_runner

_HOST_ID = "host-a"


class _Rows:
    """``get_conversation`` over preset rows, counting reads; optionally failing every read."""

    def __init__(self, *rows: Conversation, raising: bool = False) -> None:
        self.rows = {row.id: row for row in rows}
        self.reads = 0
        self.raising = raising

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        self.reads += 1
        if self.raising:
            raise RuntimeError("store unavailable")
        return self.rows.get(conversation_id)


def _row(
    conversation_id: str,
    *,
    runner_id: str | None,
    parent: str | None = None,
    root: str | None = None,
    host_id: str | None = _HOST_ID,
) -> Conversation:
    return Conversation(
        id=conversation_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=root or conversation_id,
        kind="default" if parent is None else "sub_agent",
        parent_conversation_id=parent,
        runner_id=runner_id,
        host_id=host_id,
    )


ROOT = _row("root", runner_id="runner-root")
CHILD_RIDING = _row("child", runner_id="runner-root", parent="root", root="root")
CHILD_OWN = _row("child", runner_id="runner-child", parent="root", root="root")
GRANDCHILD_ON_ROOT = _row("grandchild", runner_id="runner-root", parent="child", root="root")
GRANDCHILD_OWN = _row("grandchild", runner_id="runner-grandchild", parent="child", root="root")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conv", "rows", "owns", "reads"),
    [
        pytest.param(ROOT, [ROOT], True, 0, id="top-level session owns its runner without reads"),
        pytest.param(_row("solo", runner_id=None), [], False, 0, id="no runner binding"),
        pytest.param(_row("solo", runner_id="r", host_id=None), [], False, 0, id="no host"),
        pytest.param(CHILD_RIDING, [ROOT], False, 1, id="child riding the parent's runner"),
        pytest.param(CHILD_OWN, [ROOT], True, 1, id="child on a runner of its own"),
        pytest.param(
            GRANDCHILD_ON_ROOT, [ROOT, CHILD_OWN], False, 2, id="grandchild on the root's runner"
        ),
        pytest.param(
            GRANDCHILD_OWN, [ROOT, CHILD_OWN], True, 2, id="grandchild on a runner of its own"
        ),
        pytest.param(GRANDCHILD_OWN, [ROOT], False, 1, id="missing ancestor row fails closed"),
        pytest.param(
            CHILD_RIDING,
            [_row("root", runner_id=None)],
            False,
            1,
            id="unbound ancestor on the same host fails closed",
        ),
        pytest.param(
            CHILD_OWN,
            [_row("root", runner_id=None, host_id=None)],
            True,
            1,
            id="ancestor without a host runner does not own the child's",
        ),
        pytest.param(
            _row("loop-b", runner_id="own", parent="loop-a", root="loop-a"),
            [_row("loop-a", runner_id="other", parent="loop-b", root="loop-a")],
            False,
            1,
            id="ancestor cycle fails closed",
        ),
    ],
)
async def test_owns_host_runner(
    conv: Conversation, rows: list[Conversation], owns: bool, reads: int
) -> None:
    store = _Rows(*rows)
    assert await _owns_host_runner(conv, store) is owns  # type: ignore[arg-type]
    assert store.reads == reads


_LIMIT = sessions_helpers._OWNERSHIP_ANCESTOR_LOOKUP_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [_LIMIT, _LIMIT + 1], ids=["at the bound", "past the bound"])
async def test_owns_host_runner_reads_exactly_the_ancestor_bound(depth: int) -> None:
    """A chain of exactly the bound is verified; one more ancestor fails closed."""
    rows = [_row("a0", runner_id="runner-a0")]
    for i in range(1, depth + 1):
        rows.append(_row(f"a{i}", runner_id=f"runner-a{i}", parent=f"a{i - 1}", root="a0"))
    leaf, store = rows[-1], _Rows(*rows[:-1])
    assert await _owns_host_runner(leaf, store) is (depth <= _LIMIT)  # type: ignore[arg-type]
    assert store.reads == min(depth, _LIMIT)


@pytest.mark.asyncio
async def test_owns_host_runner_fails_closed_when_an_ancestor_read_raises() -> None:
    store = _Rows(ROOT, raising=True)
    assert await _owns_host_runner(CHILD_OWN, store) is False  # type: ignore[arg-type]
    assert store.reads == 1
