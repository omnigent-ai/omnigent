"""Delete releases the host-launched runners no remaining session is bound to."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration


def _row(session_id: str, host_id: str | None = None, runner_id: str | None = None):
    return SimpleNamespace(id=session_id, host_id=host_id, runner_id=runner_id)


class _Store:
    """Just the two reads the delete-time helpers make."""

    def __init__(
        self,
        rows: dict[str, SimpleNamespace] | None = None,
        *,
        bound_runners: set[str] = frozenset(),
        failing_lookups: set[str] = frozenset(),
        descendants_fail: bool = False,
    ) -> None:
        self.rows = rows or {}
        self.bound_runners = bound_runners
        self.failing_lookups = failing_lookups
        self.descendants_fail = descendants_fail
        self.lookups: list[tuple[str, int]] = []

    def get_conversations(self, conversation_ids: list[str]) -> dict[str, SimpleNamespace]:
        if self.descendants_fail:
            raise RuntimeError("database unavailable")
        return {cid: self.rows[cid] for cid in conversation_ids if cid in self.rows}

    def list_runner_session_statuses(
        self, runner_id: str, *, after: str | None = None, limit: int = 200
    ) -> list[tuple[str, str | None]]:
        self.lookups.append((runner_id, limit))
        if runner_id in self.failing_lookups:
            raise RuntimeError("database unavailable")
        return [("other-session", "running")] if runner_id in self.bound_runners else []


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, bool]]:
    """Record every host runner stop the helper asks for."""
    calls: list[tuple[str, str, str, bool]] = []

    async def stop(session_id, host_id, runner_id, host_registry, *, expect_already_stopped=False):
        calls.append((session_id, host_id, runner_id, expect_already_stopped))
        if runner_id == "runner-broken":
            raise RuntimeError("host did not answer")
        return True

    monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
    return calls


async def test_host_runner_bindings_cover_the_session_tree() -> None:
    parent = _row("parent", "host-1", "runner-shared")
    store = _Store(
        {
            "child-shared": _row("child-shared", "host-1", "runner-shared"),
            "child-moved": _row("child-moved", "host-1", "runner-moved"),
            "child-unbound": _row("child-unbound", None, None),
        }
    )

    bindings = await orchestration._host_runner_bindings(
        store, parent, ["child-shared", "child-moved", "child-unbound", "child-gone"]
    )

    assert bindings == {("host-1", "runner-shared"), ("host-1", "runner-moved")}


async def test_host_runner_bindings_keep_the_session_binding_when_descendants_fail() -> None:
    parent = _row("parent", "host-1", "runner-shared")
    store = _Store(descendants_fail=True)

    bindings = await orchestration._host_runner_bindings(store, parent, ["child"])

    assert bindings == {("host-1", "runner-shared")}


async def test_stop_released_host_runners_skips_a_runner_another_session_uses(
    stops: list[tuple[str, str, str, bool]],
) -> None:
    store = _Store(bound_runners={"runner-shared"})

    await orchestration._stop_released_host_runners(
        "child", {("host-1", "runner-shared"), ("host-1", "runner-moved")}, store, object()
    )

    assert store.lookups == [("runner-moved", 1), ("runner-shared", 1)]
    assert stops == [("child", "host-1", "runner-moved", True)]


async def test_stop_released_host_runners_keeps_going_past_failures(
    stops: list[tuple[str, str, str, bool]],
) -> None:
    store = _Store(failing_lookups={"runner-a"})
    bindings = {
        ("host-1", "runner-a"),  # the "still bound" check fails: left running
        ("host-1", "runner-broken"),  # the host stop raises: swallowed
        ("host-1", "runner-c"),
    }

    await orchestration._stop_released_host_runners("parent", bindings, store, object())

    assert [call[2] for call in stops] == ["runner-broken", "runner-c"]
