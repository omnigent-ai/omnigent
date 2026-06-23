"""Tests for the native Antigravity (agy) executor bridge (web-turn injection).

These pin the write path: a web/mobile turn is delivered to the running agy over
its connect-RPC ``SendUserCascadeMessage`` (``send_user_cascade_message``, mocked
here), which agy records as a real ``USER_INPUT`` step; agy's reply is mirrored
back by the read driver — so the executor yields a ``TurnComplete`` with no text
rather than fabricating a reply. Delivery is RPC for EVERY turn (the old tmux
send-keys path is retired). The RPC wire shapes are exercised in
``test_antigravity_native_rpc``; here the RPC client is stubbed so the tests
assert the executor's wiring — what text + resolved model it delivers, how it
resolves the cascade id / port, and how it maps success/failure to events.

Model resolution (two-tier, see design §10.1/§10.4): the executor echoes agy's
CURRENT model from the latest ``USER_INPUT`` step's
``userInput.userConfig.plannerConfig.requestedModel.model`` (via
``get_trajectory_steps``); on the first turn / when not yet observable it falls
back to the ``recommended`` entry from ``get_available_models``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import omnigent.inner.antigravity_native_executor as executor_mod
from omnigent.antigravity_native_bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state,
)
from omnigent.antigravity_native_rpc import AntigravityRpcError
from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent, TurnComplete

_CONVERSATION_ID = "90468e33-38c3-4e48-ae9f-03c843196227"
_PLACEHOLDER_ID = "agy_conv_placeholder123"
_PORT = 52548
_ECHOED_MODEL = "MODEL_PLACEHOLDER_M20"
_RECOMMENDED_MODEL = "MODEL_PLACEHOLDER_M132"


def _executor(tmp_path: Path) -> AntigravityNativeExecutor:
    """
    Build an executor with an explicit bridge dir (no env needed).

    :param tmp_path: Pytest temporary directory used as the bridge dir.
    :returns: A configured :class:`AntigravityNativeExecutor`.
    """
    return AntigravityNativeExecutor(bridge_dir=tmp_path)


def _seed_state(tmp_path: Path, *, conversation_id: str = _CONVERSATION_ID) -> None:
    """
    Write bridge state the executor will read before delivering.

    :param tmp_path: Bridge directory.
    :param conversation_id: agy conversation id to record (a real id, or an
        ``agy_conv_*`` placeholder to model a fresh, not-yet-discovered session).
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        AntigravityNativeBridgeState(session_id="conv_test", conversation_id=conversation_id),
    )


def _steps_with_model(model: str) -> list[dict[str, object]]:
    """
    Build a trajectory-step list whose latest USER_INPUT step carries ``model``.

    Mirrors the shape the executor reads to echo agy's current model:
    ``step.userInput.userConfig.plannerConfig.requestedModel.model`` on a
    ``CORTEX_STEP_TYPE_USER_INPUT`` step. Includes a trailing non-USER_INPUT
    step so the test exercises "find the latest USER_INPUT", not "take the last".

    :param model: agy model enum string to embed in the latest USER_INPUT step.
    :returns: A step list ending past the USER_INPUT step.
    """
    return [
        {
            "stepIndex": 0,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {
                "userConfig": {
                    "plannerConfig": {"requestedModel": {"model": "MODEL_PLACEHOLDER_OLD"}}
                }
            },
        },
        {
            "stepIndex": 1,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {"userConfig": {"plannerConfig": {"requestedModel": {"model": model}}}},
        },
        {"stepIndex": 2, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {}},
    ]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """
    Stub the RPC turn-send + discovery, recording what the executor delivers.

    Wires the executor's whole RPC surface to in-memory fakes:

    * ``resolve_language_server_port`` -> ``rec["port"]`` (``None`` models "no
      agy resolvable").
    * ``get_trajectory_steps`` -> ``rec["steps"]`` (the model-echo source).
    * ``get_available_models`` -> ``rec["models"]`` (the recommended fallback).
    * ``send_user_cascade_message`` records each ``{cascade_id, text,
      plan_model}`` call; set ``rec["send_raise"]`` to raise it.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: A mutable dict of fakes + recordings (see keys above).
    """
    rec: dict[str, object] = {
        "port": _PORT,
        "steps": _steps_with_model(_ECHOED_MODEL),
        "models": {
            "models": {
                "k1": {"model": _RECOMMENDED_MODEL, "displayName": "Rec", "recommended": True},
                "k2": {"model": "MODEL_OTHER", "displayName": "Other", "recommended": False},
            }
        },
        "send_raise": None,
        "sends": [],
        "trajectory_calls": [],
        "models_calls": 0,
    }

    def _resolve_port(conversation_id: str) -> int | None:
        del conversation_id
        port = rec["port"]
        return port if isinstance(port, int) else None

    def _get_steps(port: int, cascade_id: str) -> list[dict[str, object]]:
        calls = rec["trajectory_calls"]
        assert isinstance(calls, list)
        calls.append({"port": port, "cascade_id": cascade_id})
        steps = rec["steps"]
        return steps if isinstance(steps, list) else []

    def _get_models(port: int) -> dict[str, object]:
        del port
        prior = rec["models_calls"]
        assert isinstance(prior, int)
        rec["models_calls"] = prior + 1  # running tally
        models = rec["models"]
        return models if isinstance(models, dict) else {}

    def _send(port: int, cascade_id: str, text: str, *, plan_model: str) -> None:
        sends = rec["sends"]
        assert isinstance(sends, list)
        sends.append(
            {"port": port, "cascade_id": cascade_id, "text": text, "plan_model": plan_model}
        )
        exc = rec["send_raise"]
        if exc is not None:
            assert isinstance(exc, BaseException)
            raise exc

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "get_trajectory_steps", _get_steps)
    monkeypatch.setattr(executor_mod, "get_available_models", _get_models)
    monkeypatch.setattr(executor_mod, "send_user_cascade_message", _send)
    return rec


def _sends(rec: dict[str, object]) -> list[dict[str, object]]:
    """Return the recorded ``send_user_cascade_message`` calls, in order."""
    sends = rec["sends"]
    assert isinstance(sends, list)
    return sends


async def _run(executor: AntigravityNativeExecutor, text: str) -> list[ExecutorEvent]:
    """
    Drive ``run_turn`` with a single user message and collect its events.

    :param executor: Executor under test.
    :param text: User message text.
    :returns: The yielded executor events.
    """
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": text}],
            tools=[],
            system_prompt="",
        )
    ]


# ---------------------------------------------------------------------------
# capability flags
# ---------------------------------------------------------------------------


def test_does_not_support_streaming(tmp_path: Path) -> None:
    """
    ``supports_streaming`` is ``False``.

    Assistant output is posted by the read driver, not streamed by the executor,
    so it must report no streaming or the workflow would await chunks that never
    come.
    """
    assert _executor(tmp_path).supports_streaming() is False


def test_supports_live_message_queue(tmp_path: Path) -> None:
    """
    ``supports_live_message_queue`` is ``True``.

    The server routes mid-turn web messages to ``enqueue_session_message``; the
    executor advertises live steering so that wiring stays active under the RPC
    turn-send path.
    """
    assert _executor(tmp_path).supports_live_message_queue() is True


# ---------------------------------------------------------------------------
# run_turn — delivery (RPC turn-send)
# ---------------------------------------------------------------------------


def test_run_turn_sends_via_rpc_and_completes(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    ``run_turn`` sends the user text over RPC and yields a text-less TurnComplete.

    The executor resolves the cascade id (bridge state) + port, resolves the
    model (echoing the latest USER_INPUT step), calls
    ``send_user_cascade_message``, and yields ``TurnComplete`` with
    ``response=None`` — the read driver mirrors agy's actual reply, so
    fabricating text here would duplicate it.
    """
    _seed_state(tmp_path)
    events = asyncio.run(_run(_executor(tmp_path), "what is 2+2?"))
    assert _sends(sent) == [
        {
            "port": _PORT,
            "cascade_id": _CONVERSATION_ID,
            "text": "what is 2+2?",
            "plan_model": _ECHOED_MODEL,
        }
    ]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_run_turn_echoes_current_model_from_trajectory(
    tmp_path: Path, sent: dict[str, object]
) -> None:
    """
    The plan model is echoed from the latest USER_INPUT step (tier-1 resolution).

    The executor must reflect the user's current TUI/session model without new
    plumbing, so it reads the most recent USER_INPUT step's
    ``requestedModel.model`` and does NOT consult the catalog when that is
    available.
    """
    _seed_state(tmp_path)
    asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _sends(sent)[0]["plan_model"] == _ECHOED_MODEL
    assert sent["models_calls"] == 0, "must not hit the catalog when the model echoes"


def test_run_turn_falls_back_to_recommended_model(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    With no observable current model, the recommended catalog entry is used (tier-2).

    On a first turn there is no prior USER_INPUT step to echo, so the executor
    must resolve the model from ``get_available_models`` by picking the
    ``recommended`` entry — agy rejects a turn with no ``planModel``.
    """
    _seed_state(tmp_path)
    sent["steps"] = []  # no USER_INPUT step yet -> nothing to echo
    asyncio.run(_run(_executor(tmp_path), "first turn"))
    assert _sends(sent)[0]["plan_model"] == _RECOMMENDED_MODEL
    assert sent["models_calls"] == 1


def test_run_turn_flattens_content_blocks(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    Content-block user messages are flattened to text before delivery.

    A web turn arrives as ``input_text`` blocks; the executor must join their
    text (and drop image/file blocks the text turn cannot carry) so agy receives
    the typed prompt.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "line one"},
                            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                            {"type": "input_text", "text": "line two"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert _sends(sent)[0]["text"] == "line one\nline two"
    assert isinstance(events[0], TurnComplete)


def test_run_turn_uses_latest_user_message(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    Only the latest user message is delivered (history is not replayed).

    agy already holds the conversation history; re-sending older turns would
    duplicate them. The executor must pick the most recent user message.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new question"},
                ],
                tools=[],
                system_prompt="",
            )
        ]

    asyncio.run(_drive())
    assert _sends(sent)[0]["text"] == "new question"


def test_run_turn_no_user_text_errors(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    A turn with no user text yields an ExecutorError without sending.

    Guards against sending an empty message to agy; there is nothing to send, so
    the executor reports an error instead.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "assistant", "content": "only assistant"}],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# ---------------------------------------------------------------------------
# run_turn — first-turn (placeholder) readiness (Option A: pure RPC)
# ---------------------------------------------------------------------------


def test_run_turn_first_turn_waits_for_discovered_id(
    tmp_path: Path, sent: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    On a placeholder (fresh) session, the turn waits for the real id, then sends.

    Under pure-RPC turn-send the executor never types into the TUI to mint the
    conversation (the runner does that — Task 11). It instead waits for the
    forwarder/runner to overwrite the ``agy_conv_*`` placeholder with agy's real
    id, then sends to that id. Modeled here by flipping bridge state to the real
    id after the first read.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_INTERVAL_S", 0.0)
    flip = {"done": False}

    def _read(bridge_dir: Path) -> AntigravityNativeBridgeState | None:
        del bridge_dir
        # First read returns the placeholder; subsequent reads return the real id
        # (as if the runner discovered + persisted it).
        if not flip["done"]:
            flip["done"] = True
            return AntigravityNativeBridgeState(
                session_id="conv_test", conversation_id=_PLACEHOLDER_ID
            )
        return AntigravityNativeBridgeState(
            session_id="conv_test", conversation_id=_CONVERSATION_ID
        )

    monkeypatch.setattr(executor_mod, "read_bridge_state", _read)
    events = asyncio.run(_run(_executor(tmp_path), "first hello"))
    assert _sends(sent) == [
        {
            "port": _PORT,
            "cascade_id": _CONVERSATION_ID,
            "text": "first hello",
            "plan_model": _ECHOED_MODEL,
        }
    ]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)


def test_run_turn_first_turn_not_ready_errors(
    tmp_path: Path, sent: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    If the real id never lands, the turn surfaces a clear "not ready" error.

    Pure-RPC turn-send cannot deliver to the ``agy_conv_*`` placeholder, so when
    the runner has not yet minted agy's real conversation within the wait window
    the executor errors gracefully (no crash, no send) rather than RPC-ing a
    placeholder. The wait is shortened so the test does not block.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_INTERVAL_S", 0.0)
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "not ready" in events[0].message


# ---------------------------------------------------------------------------
# run_turn — failure mapping
# ---------------------------------------------------------------------------


def test_run_turn_missing_state_errors(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    With no bridge state, ``run_turn`` yields an ExecutorError (no send).

    The runner seeds bridge state before launching the terminal, so a missing
    state file means broken wiring and the executor reads it once and errors
    immediately (no polling, no send).
    """
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "bridge state is missing" in events[0].message


def test_run_turn_inactive_session_errors(
    tmp_path: Path, sent: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A mismatched request session id blocks delivery with an ExecutorError.

    The harness only steers the conversation it was spawned for; if the bridge
    state names a different session, delivery must be refused.
    """
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    monkeypatch.setattr(executor, "_request_session_id", "conv_other")
    events = asyncio.run(_run(executor, "hi"))
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active" in events[0].message


def test_run_turn_no_port_errors(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    When no agy connect-RPC port resolves, ``run_turn`` errors (no send).

    The turn cannot be delivered to an agy that cannot be located on the loopback
    socket table, so the executor reports a clear error rather than a false
    success.
    """
    _seed_state(tmp_path)
    sent["port"] = None
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


def test_run_turn_rpc_error_surfaces(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    An ``AntigravityRpcError`` from the turn-send surfaces as an ExecutorError.

    A model/validation error (e.g. "neither PlanModel nor RequestedModel
    specified") or a transport failure from ``send_user_cascade_message`` must be
    propagated — carrying agy's message — not silently swallowed into a fake
    success.
    """
    _seed_state(tmp_path)
    sent["send_raise"] = AntigravityRpcError("neither PlanModel nor RequestedModel specified")
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "neither PlanModel nor RequestedModel specified" in events[0].message


# ---------------------------------------------------------------------------
# enqueue_session_message (mid-turn steering)
# ---------------------------------------------------------------------------


def test_enqueue_session_message_delivers(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    ``enqueue_session_message`` sends the steer via the same RPC path and returns True.

    Mid-turn web steering reuses the RPC turn-send, so a successful enqueue must
    deliver the content and report success.
    """
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer me"))
    assert result is True
    assert _sends(sent)[0]["text"] == "steer me"


def test_enqueue_session_message_empty_returns_false(
    tmp_path: Path, sent: dict[str, object]
) -> None:
    """
    Enqueuing empty content returns False without sending.

    There is nothing to steer with, so the executor reports it did nothing.
    """
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", ""))
    assert result is False
    assert _sends(sent) == []


def test_enqueue_session_message_rpc_failure_returns_false(
    tmp_path: Path, sent: dict[str, object]
) -> None:
    """
    A failed RPC send during enqueue returns False.

    Mid-turn steering is best-effort; a failed turn-send must be reported as not
    delivered.
    """
    _seed_state(tmp_path)
    sent["send_raise"] = AntigravityRpcError("boom")
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer"))
    assert result is False


# ---------------------------------------------------------------------------
# interrupt_session (real interrupt via CancelCascadeSteps)
# ---------------------------------------------------------------------------


def test_interrupt_session_cancels_and_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``interrupt_session`` resolves the port + cascade id and cancels, returning True.

    A successful ``cancel_cascade_steps`` against the discovered agy means the
    running cascade was asked to stop, so the executor reports the interrupt
    succeeded.
    """
    _seed_state(tmp_path)
    seen: dict[str, object] = {}

    def _resolve_port(conversation_id: str) -> int | None:
        seen["resolved_for"] = conversation_id
        return _PORT

    def _cancel(port: int, cascade_id: str) -> bool:
        seen["cancel"] = {"port": port, "cascade_id": cascade_id}
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is True
    assert seen["resolved_for"] == _CONVERSATION_ID
    assert seen["cancel"] == {"port": _PORT, "cascade_id": _CONVERSATION_ID}


def test_interrupt_session_rpc_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failed ``cancel_cascade_steps`` makes ``interrupt_session`` return False.

    ``cancel_cascade_steps`` fails open (returns False) on any RPC/transport
    error, and the executor must honestly relay that the interrupt did not land
    rather than claiming success.
    """
    _seed_state(tmp_path)
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: _PORT)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", lambda _port, _cid: False)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False


def test_interrupt_session_no_port_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no resolvable agy port, ``interrupt_session`` returns False without cancelling.

    A turn cannot be interrupted on an agy that cannot be located, so the
    executor reports failure and never calls cancel.
    """
    _seed_state(tmp_path)
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: None)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_session_placeholder_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    On a placeholder (no real conversation yet), interrupt returns False, no cancel.

    There is no live cascade to cancel before agy has minted its real id, so the
    executor must not RPC against the ``agy_conv_*`` placeholder.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    called = {"resolve": False, "cancel": False}

    def _resolve_port(_conv: str) -> int | None:
        called["resolve"] = True
        return _PORT

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_session_missing_state_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no bridge state, ``interrupt_session`` returns False without cancelling.

    No bridge state means no cascade id to address, so the interrupt is a no-op
    reported as failure.
    """
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


# ---------------------------------------------------------------------------
# model resolution helpers
# ---------------------------------------------------------------------------


def test_latest_requested_model_picks_latest_user_input() -> None:
    """
    ``_latest_requested_model`` returns the most recent USER_INPUT step's model.

    Echoing agy's CURRENT model means scanning for the LAST USER_INPUT step
    (a later turn may have switched models), not the first or the last step.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model(_steps_with_model(_ECHOED_MODEL)) == _ECHOED_MODEL


def test_latest_requested_model_none_when_absent() -> None:
    """
    ``_latest_requested_model`` returns ``None`` when no USER_INPUT model is present.

    An empty step list (first turn) or steps without a ``requestedModel`` must
    signal "nothing to echo" so the caller falls back to the recommended model.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model([]) is None
    assert (
        _latest_requested_model([{"stepIndex": 0, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE"}])
        is None
    )


def test_recommended_model_picks_recommended_entry() -> None:
    """
    ``_recommended_model`` returns the ``recommended`` catalog entry's enum.

    The fallback model must be the one agy marks ``recommended`` so a first turn
    uses agy's own default rather than an arbitrary catalog entry.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    catalog: dict[str, object] = {
        "models": {
            "a": {"model": "MODEL_A", "recommended": False},
            "b": {"model": "MODEL_B", "recommended": True},
        }
    }
    assert _recommended_model(catalog) == "MODEL_B"


def test_recommended_model_none_when_absent() -> None:
    """
    ``_recommended_model`` returns ``None`` when no entry is recommended.

    A catalog with no ``recommended`` model (or a malformed one) must signal
    "no model" so the caller surfaces a clear error instead of guessing.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    assert _recommended_model({"models": {}}) is None
    assert _recommended_model({"models": {"a": {"model": "MODEL_A"}}}) is None
    assert _recommended_model({}) is None


def test_run_turn_no_model_resolvable_errors(tmp_path: Path, sent: dict[str, object]) -> None:
    """
    When neither tier yields a model, ``run_turn`` errors without sending.

    agy requires a ``planModel`` per turn; if the trajectory has no model to echo
    AND the catalog has no recommended entry, the executor cannot construct a
    valid turn and must surface an error rather than send an invalid request.
    """
    _seed_state(tmp_path)
    sent["steps"] = []
    sent["models"] = {"models": {}}
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _sends(sent) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_init_requires_bridge_dir_env_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Constructing without a bridge dir or env var raises ``RuntimeError``.

    The harness always spawns with ``HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR``
    set; a missing value means the runner wiring is broken, which must fail loud
    rather than read a bogus path.
    """
    monkeypatch.delenv("HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"):
        AntigravityNativeExecutor()


# ---------------------------------------------------------------------------
# reasoning_effort validation (F-M5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_run_turn_valid_effort_is_accepted(
    tmp_path: Path, sent: dict[str, object], effort: str
) -> None:
    """
    A valid Antigravity effort level (low/medium/high) does not block delivery.

    agy's Gemini backend supports these three levels. A valid effort in the
    config must not surface as an error — the executor validates it and proceeds
    to send.

    :param tmp_path: Bridge directory (injected by pytest).
    :param sent: Stub recording RPC turn-sends.
    :param effort: One valid effort level to test.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)


@pytest.mark.parametrize("bad_effort", ["xhigh", "max", "none", "minimal"])
def test_run_turn_unsupported_effort_surfaces_error(
    tmp_path: Path, sent: dict[str, object], bad_effort: str
) -> None:
    """
    An effort level unsupported by Antigravity/Gemini yields an ExecutorError.

    ``xhigh`` and ``max`` are OpenAI/Anthropic-only; ``none`` and ``minimal``
    are OpenAI-only. Passing them to an Antigravity turn should surface a
    clear non-retryable error so the caller does not silently ignore the
    mismatch.

    :param tmp_path: Bridge directory.
    :param sent: Stub recording RPC turn-sends.
    :param bad_effort: An effort level that is invalid for Antigravity.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": bad_effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert _sends(sent) == [], "delivery must not happen on bad effort"
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert bad_effort in events[0].message


# ---------------------------------------------------------------------------
# _content_to_text flattening
# ---------------------------------------------------------------------------


def test_content_to_text_handles_string_blocks_none_and_other() -> None:
    """
    Flattening covers every content shape the executor may receive.

    A plain string passes through; ``input_text``/``text`` blocks join by newline
    while image/file blocks are dropped (the text turn is text-only); ``None``
    yields ``""``; any other shape falls back to a JSON encoding rather than
    crashing.
    """
    from omnigent.inner.antigravity_native_executor import _content_to_text

    assert _content_to_text("  hello  ") == "hello"
    assert (
        _content_to_text(
            [
                {"type": "input_text", "text": "a"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                {"type": "text", "text": "b"},
            ]
        )
        == "a\nb"
    )
    assert _content_to_text(None) == ""
    # Defensive fallback for an unexpected shape: encoded, not crashed.
    assert _content_to_text(123) == "123"
