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

from omnigent.errors import OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes._sessions import orchestration as orchestration_mod
from omnigent.server.routes.sessions import (
    _deliver_subagent_terminal_status_with_recovery,
    _recover_subagent_status_forward_via_parent,
    _require_external_status_forward,
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

    def __init__(
        self,
        parent: Any | None,
        *,
        ancestors: list[Any] | None = None,
        raise_on_rebind: bool = False,
    ) -> None:
        self._conversations = {
            conv.id: conv for conv in ([parent] if parent is not None else []) + (ancestors or [])
        }
        self._raise_on_rebind = raise_on_rebind
        self.rebinds: list[tuple[str, str]] = []

    def get_conversation(self, conversation_id: str) -> Any | None:
        return self._conversations.get(conversation_id)

    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Any:
        if self._raise_on_rebind:
            # Simulate the child row being deleted between post_event reading
            # it and this heal (a mid-teardown race).
            raise ConversationNotFoundError(conversation_id)
        self.rebinds.append((conversation_id, runner_id))
        return _conv(conversation_id, runner_id=runner_id)


class _FakeRunnerClient:
    """Stand-in for the resolved parent-runner ``httpx.AsyncClient``.

    Recovery now threads this SAME resolved client through to both the
    session-init handshake and the terminal-status re-forward (rather than
    re-resolving a client by child id for the re-forward), so tests drive it
    directly instead of monkeypatching ``_forward_session_change_to_runner``.
    """

    def __init__(self, response: httpx.Response | BaseException | None = None) -> None:
        self._response = response if response is not None else httpx.Response(202)
        self.posted: list[str] = []

    async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
        self.posted.append(url)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


@pytest.fixture
def _patch_wait_and_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Stub ``_wait_for_runner_client`` and ``_ensure_runner_session_initialized``.

    Returns a mutable dict the test tunes (``wait_returns`` — a
    :class:`_FakeRunnerClient` or ``None``) and reads back (``waited_for``,
    ``init_calls``). The re-forward posts through ``wait_returns`` directly,
    so its ``.posted`` list is what a test asserts on for the retry.
    """
    state: dict[str, Any] = {
        "wait_returns": _FakeRunnerClient(),
        "waited_for": [],
        "init_calls": [],
        "init_result": False,
    }

    async def _fake_wait(session_id: str, *_a: Any, **_k: Any) -> Any:
        state["waited_for"].append(session_id)
        return state["wait_returns"]

    async def _fake_init(session_id: str, conv: Any, client: Any, conv_store: Any) -> bool:
        del conv, client, conv_store
        state["init_calls"].append(session_id)
        return state["init_result"]

    monkeypatch.setattr(sessions_mod, "_wait_for_runner_client", _fake_wait)
    # ``_ensure_runner_session_initialized`` is a direct implementation (not a
    # call-time facade proxy), so it must be patched where it's actually
    # called from — orchestration's own module globals — not on the
    # re-exported ``sessions`` facade.
    monkeypatch.setattr(orchestration_mod, "_ensure_runner_session_initialized", _fake_init)
    return state


async def test_recover_rebinds_to_parent_runner_and_redelivers(
    _patch_wait_and_init: dict[str, Any],
) -> None:
    """
    Stale child id heals to the parent's live runner and the forward re-lands.

    This is the core production fix: the child was pinned to ``runner_old``
    (now dead), the parent has since rebound to ``runner_new``. Recovery must
    rebind the child to ``runner_new`` and re-POST the terminal status
    through the SAME resolved parent-runner client, returning the 202.
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
    # ...and the retry posted through the resolved parent client, to the
    # child's own events URL.
    assert _patch_wait_and_init["wait_returns"].posted == ["/v1/sessions/conv_child/events"]
    assert _patch_wait_and_init["waited_for"] == ["conv_parent"]


async def test_recover_gives_up_when_parent_runner_never_connects(
    _patch_wait_and_init: dict[str, Any],
) -> None:
    """
    If the parent's runner tunnel never (re)connects, recovery returns None.

    The caller then fails the forward as before (a 503 the runner retries) —
    we must NOT rebind or forward against a runner we couldn't confirm live.
    """
    _patch_wait_and_init["wait_returns"] = None  # tunnel never comes up
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


async def test_recover_no_parent_returns_none(
    _patch_wait_and_init: dict[str, Any],
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
    assert _patch_wait_and_init["wait_returns"].posted == []


async def test_recover_same_runner_skips_rebind_but_retries(
    _patch_wait_and_init: dict[str, Any],
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
    assert _patch_wait_and_init["wait_returns"].posted == ["/v1/sessions/conv_child/events"]


async def test_recover_deleted_child_race_degrades_to_none(
    _patch_wait_and_init: dict[str, Any],
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
    # The deleted-child race short-circuits before any retry post.
    assert _patch_wait_and_init["wait_returns"].posted == []


async def test_recover_falls_back_to_root_when_no_direct_parent(
    _patch_wait_and_init: dict[str, Any],
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
    assert _patch_wait_and_init["waited_for"] == ["conv_root"]


async def test_recover_real_body_retry_resolves_healed_runner() -> None:
    """
    End-to-end recovery body: the healed ``runner_id`` is what the retry resolves.

    Drives the REAL recovery path with no stub of the client-resolution
    internals below ``runner_router``/``_get_runner_client``. A fake router
    mirrors ``RunnerRouter``'s contract — it re-reads the conversation's
    current ``runner_id`` fresh on every resolve and only hands back a client
    for the live runner. This asserts the load-bearing invariant flagged in
    review: after ``replace_runner_id`` heals the child onto the parent's
    live runner, the resolved PARENT client (not a re-resolve by child id) is
    what the retry posts through, and it lands (202).
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
        # rebind + resolve-once + post path. ``_ensure_runner_session_initialized``
        # is not stubbed either: it raises AttributeError building the init
        # payload (the SimpleNamespace stand-in has no agent_id), which the
        # recovery function's best-effort wrapper swallows — so the ONLY
        # request the mock transport should see is the real re-forward.
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
    # ...and the retry posted through the resolved PARENT client, to the
    # child's events URL — the resolver was consulted exactly once (for the
    # parent), not re-consulted for the child.
    assert posted == ["/v1/sessions/conv_child/events"]


async def test_recover_runnerless_parent_uses_live_root_client(
    _patch_wait_and_init: dict[str, Any],
) -> None:
    """A runnerless immediate parent remains the handshake target via a live root."""
    child = _conv(
        "conv_child",
        runner_id="runner_old",
        parent_id="conv_parent",
        root_id="conv_root",
    )
    parent = _conv("conv_parent", runner_id=None, root_id="conv_root")
    root = _conv("conv_root", runner_id="runner_live")
    store = _FakeStore(parent, ancestors=[root])

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    assert _patch_wait_and_init["waited_for"] == ["conv_root"]
    assert _patch_wait_and_init["init_calls"] == ["conv_parent"]
    assert store.rebinds == [("conv_child", "runner_live")]


async def test_recover_parent_dead_falls_back_to_live_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery walks past a dead immediate-parent runner to the live root."""
    child = _conv(
        "conv_child",
        runner_id="runner_old",
        parent_id="conv_parent",
        root_id="conv_root",
    )
    parent = _conv("conv_parent", runner_id="runner_dead", root_id="conv_root")
    root = _conv("conv_root", runner_id="runner_live")
    store = _FakeStore(parent, ancestors=[root])
    live_client = _FakeRunnerClient()
    waited_for: list[str] = []
    init_calls: list[str] = []

    async def _fake_wait(session_id: str, *_a: Any, **_k: Any) -> Any:
        waited_for.append(session_id)
        return live_client if session_id == "conv_root" else None

    async def _fake_init(session_id: str, *_a: Any, **_k: Any) -> bool:
        init_calls.append(session_id)
        return False

    monkeypatch.setattr(sessions_mod, "_wait_for_runner_client", _fake_wait)
    monkeypatch.setattr(orchestration_mod, "_ensure_runner_session_initialized", _fake_init)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    assert waited_for == ["conv_parent", "conv_root"]
    assert init_calls == ["conv_parent"]
    assert store.rebinds == [("conv_child", "runner_live")]


async def test_delivery_calls_canonical_heal_once_across_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight and reactive recovery share one ancestor walk per attempt."""
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    parent = _conv("conv_parent", runner_id="runner_live")
    store = _FakeStore(parent)
    live_client = _FakeRunnerClient()
    heal_calls: list[str] = []

    async def _fake_heal(child_conv: Any, *_a: Any, **_k: Any) -> Any:
        heal_calls.append(child_conv.id)
        return live_client

    async def _fake_forward(*_a: Any, **_k: Any) -> None:
        return None

    async def _fake_init(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(orchestration_mod, "_heal_subagent_runner_binding_via_parent", _fake_heal)
    monkeypatch.setattr(sessions_mod, "_forward_session_change_to_runner", _fake_forward)
    monkeypatch.setattr(orchestration_mod, "_ensure_runner_session_initialized", _fake_init)

    result = await _deliver_subagent_terminal_status_with_recovery(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    assert heal_calls == ["conv_child"]
    assert live_client.posted == ["/v1/sessions/conv_child/events"]


async def test_recover_drives_parent_session_init_before_reforward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Recovery drives the parent's session-init handshake before re-forwarding.

    This is what actually seeds the parent's inbox/async-task pair on a
    runner that never ran ``create_session`` for it (a cold parent — e.g. an
    intermediate parent that is itself a nested sub-agent). Without this call,
    healing the child's ``runner_id`` alone would just re-target a runner
    that's equally unaware of the parent. A shared ``events`` log proves the
    ORDER — init strictly before the terminal-status post — not just that
    both happened.
    """
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    parent = _conv("conv_parent", runner_id="runner_new")
    store = _FakeStore(parent)

    events: list[str] = []

    async def _fake_init(session_id: str, conv: Any, client: Any, conv_store: Any) -> bool:
        del conv, client, conv_store
        events.append(f"init:{session_id}")
        return False

    class _OrderedClient(_FakeRunnerClient):
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            events.append(f"post:{url}")
            return await super().post(url, **kwargs)

    fake_client = _OrderedClient()

    async def _fake_wait(session_id: str, *_a: Any, **_k: Any) -> Any:
        return fake_client

    monkeypatch.setattr(sessions_mod, "_wait_for_runner_client", _fake_wait)
    monkeypatch.setattr(orchestration_mod, "_ensure_runner_session_initialized", _fake_init)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 202
    # The parent (not the child) had its session-init handshake driven,
    # strictly before the terminal-status re-forward landed on the SAME client.
    assert events == ["init:conv_parent", "post:/v1/sessions/conv_child/events"]


async def test_recover_success_gated_on_reforward_not_init_bool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A truthy ``_ensure_runner_session_initialized`` must not fake success.

    ``_ensure_runner_session_initialized`` conflates ``initialized`` with
    ``terminal_ready`` and swallows transport errors, so recovery must never
    treat its return value as confirmation. Here it returns ``True`` while the
    actual re-forward comes back non-2xx: recovery must surface the real
    503, and :func:`_require_external_status_forward` must still raise.
    """
    child = _conv("conv_child", runner_id="runner_old", parent_id="conv_parent")
    parent = _conv("conv_parent", runner_id="runner_new")
    store = _FakeStore(parent)

    async def _fake_init(*_a: Any, **_k: Any) -> bool:
        return True  # truthy handshake result — must NOT be trusted alone

    fake_client = _FakeRunnerClient(httpx.Response(503, text='{"error": "still_offline"}'))

    async def _fake_wait(session_id: str, *_a: Any, **_k: Any) -> Any:
        return fake_client

    monkeypatch.setattr(sessions_mod, "_wait_for_runner_client", _fake_wait)
    monkeypatch.setattr(orchestration_mod, "_ensure_runner_session_initialized", _fake_init)

    result = await _recover_subagent_status_forward_via_parent(
        child,
        runner_router=None,
        tunnel_registry=object(),
        conversation_store=store,  # type: ignore[arg-type]
        forward_body={"type": "external_session_status", "data": {"status": "idle"}},
    )

    assert result is not None and result.status_code == 503
    with pytest.raises(OmnigentError):
        _require_external_status_forward("conv_child", "idle", result)


@pytest.mark.parametrize("status_code", [100, 101, 204, 300, 302, 304])
def test_require_external_status_forward_accepts_only_strict_2xx(status_code: int) -> None:
    """
    ``_require_external_status_forward`` must reject 1xx/3xx, not just >=400.

    Only a genuine 2xx counts as delivery confirmation. 204 is the one 2xx
    exercised elsewhere and must stay accepted; the rest are non-2xx classes
    that a runner could plausibly reply with (an informational 100/101, or a
    redirect a misbehaving proxy injects) and must NOT be treated as
    "delivered" — the parent inbox never actually received anything.
    """
    result = _RunnerForwardResult(status_code=status_code, body="")
    if status_code == 204:
        _require_external_status_forward("conv_child", "idle", result)  # must not raise
        return
    with pytest.raises(OmnigentError):
        _require_external_status_forward("conv_child", "idle", result)


def test_require_external_status_forward_accepts_200_and_202() -> None:
    """Sanity check: the real 2xx codes the runner actually returns still pass."""
    for status_code in (200, 202):
        _require_external_status_forward(
            "conv_child", "idle", _RunnerForwardResult(status_code=status_code, body="")
        )
