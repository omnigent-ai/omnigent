"""Unit tests for sub-agent terminal-status forward recovery.

Covers :func:`_recover_subagent_status_forward_via_parent`, the server-side
heal for the production hang where a native sub-agent child's ``runner_id``
goes stale after its runner is relaunched under a new id (only the parent is
rebound), so the child's terminal ``idle``/``failed`` forward 503s forever and
the parent never receives the child result.
"""

from __future__ import annotations

import types
from typing import Any

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import (
    _recover_subagent_status_forward_via_parent,
    _RunnerForwardResult,
)
from omnigent.stores.conversation_store import ConversationNotFoundError


def _conv(
    conv_id: str,
    *,
    runner_id: str | None,
    parent_id: str | None = None,
    root_id: str | None = None,
) -> Any:
    """Build a minimal conversation stand-in with the fields the helper reads."""
    return types.SimpleNamespace(
        id=conv_id,
        runner_id=runner_id,
        parent_conversation_id=parent_id,
        root_conversation_id=root_id or conv_id,
    )


class _FakeStore:
    """Records ``replace_runner_id`` calls and serves a fixed parent."""

    def __init__(self, parent: Any | None, *, raise_on_rebind: bool = False) -> None:
        self._parent = parent
        self._raise_on_rebind = raise_on_rebind
        self.rebinds: list[tuple[str, str]] = []

    def get_conversation(self, conversation_id: str) -> Any | None:
        if self._parent is not None and conversation_id == self._parent.id:
            return self._parent
        return None

    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Any:
        if self._raise_on_rebind:
            # Simulate the child row being deleted between post_event reading
            # it and this heal (a mid-teardown race).
            raise ConversationNotFoundError(conversation_id)
        self.rebinds.append((conversation_id, runner_id))
        return _conv(conversation_id, runner_id=runner_id)


@pytest.fixture
def _patch_forward_and_wait(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Stub ``_forward_session_change_to_runner`` and ``_wait_for_runner_client``.

    Returns a mutable dict the test tunes (``wait_returns`` / ``forward_result``)
    and reads back (``forwarded_with`` — the session id the retry forwarded to).
    """
    state: dict[str, Any] = {
        "wait_returns": object(),  # truthy "client" by default
        "forward_result": _RunnerForwardResult(status_code=202, body=""),
        "forwarded_with": [],
        "waited_for": [],
    }

    async def _fake_wait(session_id: str, *_a: Any, **_k: Any) -> Any:
        state["waited_for"].append(session_id)
        return state["wait_returns"]

    async def _fake_forward(session_id: str, *_a: Any, **_k: Any) -> Any:
        state["forwarded_with"].append(session_id)
        return state["forward_result"]

    monkeypatch.setattr(sessions_mod, "_wait_for_runner_client", _fake_wait)
    monkeypatch.setattr(sessions_mod, "_forward_session_change_to_runner", _fake_forward)
    return state


async def test_recover_rebinds_to_parent_runner_and_redelivers(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    Stale child id heals to the parent's live runner and the forward re-lands.

    This is the core production fix: the child was pinned to ``runner_old``
    (now dead), the parent has since rebound to ``runner_new``. Recovery must
    rebind the child to ``runner_new`` and re-POST the terminal status THROUGH
    the child id (which now resolves to the live runner), returning the 202.
    """
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    parent = _conv("conv_parent", runner_id="runner_new")
    store = _FakeStore(parent)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),  # non-None → the wait path runs
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    # Child healed to the parent's current runner...
    assert store.rebinds == [("conv_child", "runner_new")]
    # ...and the retry forwarded through the (now-rebound) CHILD id.
    assert _patch_forward_and_wait["forwarded_with"] == ["conv_child"]
    assert _patch_forward_and_wait["waited_for"] == ["conv_parent"]


async def test_recover_gives_up_when_parent_runner_never_connects(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    If the parent's runner tunnel never (re)connects, recovery returns None.

    The caller then fails the forward as before (a 503 the runner retries) —
    we must NOT rebind or forward against a runner we couldn't confirm live.
    """
    _patch_forward_and_wait["wait_returns"] = None  # tunnel never comes up
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    store = _FakeStore(_conv("conv_parent", runner_id="runner_new"))

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is None
    assert store.rebinds == []
    assert _patch_forward_and_wait["forwarded_with"] == []


async def test_recover_no_parent_returns_none(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    A child with no resolvable parent (root == self) cannot be recovered.

    Guards against a top-level session that was mislabeled, or a child whose
    root points at itself — neither has a distinct parent runner to heal to.
    """
    child = _conv("conv_orphan", runner_id="runner_old", parent_id=None, root_id="conv_orphan")
    store = _FakeStore(None)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is None
    assert store.rebinds == []
    assert _patch_forward_and_wait["forwarded_with"] == []


async def test_recover_same_runner_skips_rebind_but_retries(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    A transient gap (child and parent share the SAME live id) retries, no rebind.

    Here the runner reconnected under its stable id, so the child's binding is
    already correct — recovery should NOT issue a needless ``replace_runner_id``
    but should still re-forward after waiting out the reconnect gap.
    """
    child = _conv("conv_child", runner_id="runner_same", parent_id="conv_parent")
    store = _FakeStore(_conv("conv_parent", runner_id="runner_same"))

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    assert store.rebinds == []  # same id → no rebind
    assert _patch_forward_and_wait["forwarded_with"] == ["conv_child"]


async def test_recover_deleted_child_race_degrades_to_none(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    A child deleted mid-heal degrades to ``None`` (→ 503), never a 500.

    If the child row is removed between ``post_event`` reading it and the
    rebind, ``replace_runner_id`` raises ``ConversationNotFoundError``. Recovery
    is best-effort: it must swallow that benign race and return ``None`` so the
    caller falls through to the existing 503/no-op, not surface an unhandled
    500. (Polly review note on PR #1446.)
    """
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    store = _FakeStore(_conv("conv_parent", runner_id="runner_new"), raise_on_rebind=True)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is None
    assert store.rebinds == []
    # The deleted-child race short-circuits before any retry forward.
    assert _patch_forward_and_wait["forwarded_with"] == []


async def test_recover_falls_back_to_root_when_no_direct_parent(
    _patch_forward_and_wait: dict[str, Any],
) -> None:
    """
    When ``parent_conversation_id`` is unset, recovery resolves via ``root``.

    A child persisted without a direct parent pointer (older rows / codex
    nesting) still belongs to a root conversation whose runner is the live one.
    """
    child = _conv("conv_child", runner_id="runner_old", parent_id=None, root_id="conv_root")
    store = _FakeStore(_conv("conv_root", runner_id="runner_new"))

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    assert store.rebinds == [("conv_child", "runner_new")]
    assert _patch_forward_and_wait["waited_for"] == ["conv_root"]


async def test_recover_real_body_retry_resolves_healed_runner() -> None:
    """
    End-to-end recovery body: the healed ``runner_id`` is what the retry resolves.

    Drives the REAL recovery path with NO stub of
    ``_forward_session_change_to_runner``. A fake router mirrors
    ``RunnerRouter``'s contract — it re-reads the conversation's current
    ``runner_id`` fresh on every resolve and only hands back a client for the
    live runner. This asserts the load-bearing invariant flagged in review:
    after ``replace_runner_id`` heals the child onto the parent's live runner,
    the retry's resolver picks the NEW runner and the forward lands (202) — i.e.
    healing the persisted binding genuinely repoints the retry, rather than
    resolving a stale in-memory id.
    """
    convs: dict[str, Any] = {
        "conv_parent": _conv("conv_parent", runner_id="R_live"),
        "conv_child": _conv("conv_child", runner_id="R_old", parent_id="conv_parent"),
    }
    rebinds: list[tuple[str, str]] = []

    class _Store:
        def get_conversation(self, conversation_id: str) -> Any | None:
            return convs.get(conversation_id)

        def replace_runner_id(self, conversation_id: str, runner_id: str) -> Any:
            rebinds.append((conversation_id, runner_id))
            prev = convs[conversation_id]
            convs[conversation_id] = _conv(
                conversation_id,
                runner_id=runner_id,
                parent_id=prev.parent_conversation_id,
                root_id=prev.root_conversation_id,
            )
            return convs[conversation_id]

    posted: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(202)

    live_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    )

    class _Router:
        """Re-reads the conv's CURRENT runner_id; resolves only the live one."""

        def client_for_session_resources(self, conversation_id: str) -> Any:
            runner_id = convs[conversation_id].runner_id
            if runner_id != "R_live":
                raise LookupError(f"runner {runner_id} offline")
            return types.SimpleNamespace(client=live_client)

    try:
        # tunnel_registry=None → skip the liveness wait and exercise the real
        # rebind + forward + resolver path.
        result = await _recover_subagent_status_forward_via_parent(
            convs["conv_child"],
            runner_router=_Router(),  # type: ignore[arg-type]
            tunnel_registry=None,
            conversation_store=_Store(),  # type: ignore[arg-type]
            forward_body={"type": "external_session_status", "data": {"status": "idle"}},
        )
    finally:
        await live_client.aclose()

    assert result is not None and result.status_code == 202
    # Child healed to the parent's live runner...
    assert rebinds == [("conv_child", "R_live")]
    # ...and the retry forwarded via the CHILD id, which the resolver — reading
    # the freshly healed runner_id — routed to the live runner.
    assert posted == ["/v1/sessions/conv_child/events"]
