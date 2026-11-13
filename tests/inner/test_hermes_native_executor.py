"""Unit tests for HermesNativeExecutor — the harness-side tmux injector."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner import hermes_native_executor as hne
from omnigent.inner.executor import ExecutorError, TurnComplete


def test_supports_flags(tmp_path: Path) -> None:
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True


def test_content_to_text_plain_and_parts(tmp_path: Path) -> None:
    assert hne._content_to_text("hello", tmp_path) == "hello"
    blocks = [{"type": "input_text", "text": "a"}, {"type": "text", "text": "b"}]
    assert hne._content_to_text(blocks, tmp_path) == "a\n\nb"


def test_latest_user_text_picks_last_user(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert hne._latest_user_text(messages, tmp_path) == "second"


def test_bridge_dir_from_env_requires_var(monkeypatch) -> None:
    monkeypatch.delenv(hne.BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        hne._bridge_dir_from_env()


async def test_run_turn_injects_latest_user_message(tmp_path: Path, monkeypatch) -> None:
    injected: list[tuple[Path, str]] = []

    def _fake_inject(bridge_dir: Path, *, content: str) -> None:
        injected.append((bridge_dir, content))

    monkeypatch.setattr(hne, "inject_user_message", _fake_inject)
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "do it"}], [], "")]
    assert injected == [(tmp_path, "do it")]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)


async def test_run_turn_errors_with_no_user_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hne, "inject_user_message", lambda *a, **k: None)
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "assistant", "content": "x"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


async def test_enqueue_session_message_injects(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        hne, "inject_user_message", lambda bridge_dir, *, content: seen.append(content)
    )
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert await ex.enqueue_session_message("main", "steer") is True
    assert seen == ["steer"]
    # Empty content is a no-op (no injection).
    assert await ex.enqueue_session_message("main", "") is False


async def test_interrupt_session_success(tmp_path: Path, monkeypatch) -> None:
    called: list[Path] = []
    monkeypatch.setattr(
        hne, "inject_interrupt", lambda bridge_dir, **kw: called.append(bridge_dir)
    )
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert await ex.interrupt_session("main") is True
    assert called == [tmp_path]


async def test_interrupt_session_failure(tmp_path: Path, monkeypatch) -> None:
    def _fail(bridge_dir, **kw):
        raise RuntimeError("tmux not available")

    monkeypatch.setattr(hne, "inject_interrupt", _fail)
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert await ex.interrupt_session("main") is False


@pytest.mark.parametrize("steering", [False, True])
async def test_unconfirmed_delivery_preserves_native_work_and_late_input_matching(
    tmp_path: Path, monkeypatch, db_uri: str, steering: bool
) -> None:
    import asyncio
    import contextlib
    import uuid
    from unittest.mock import Mock

    from omnigent.runtime import pending_inputs, session_stream
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
    from omnigent.server.schemas import (
        CreateResponseRequest,
        InjectionConsumedEvent,
        SessionEventInput,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    sent = []
    interrupted = []

    def inject(_bridge, *, content):
        sent.append(content)
        raise hne.HermesDeliveryUnconfirmed("delivery is unconfirmed")

    monkeypatch.setattr(hne, "inject_user_message", inject)
    monkeypatch.setattr(hne, "inject_interrupt", lambda *_a, **_k: interrupted.append(True))
    publish = Mock()
    monkeypatch.setattr(session_stream, "publish", publish)
    executor = hne.HermesNativeExecutor(tmp_path)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    ctx = TurnContext(
        response_id="resp_pending", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )
    request = CreateResponseRequest(model="test-agent", input="delayed first input")
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="Test", labels={"omnigent.wrapper": "hermes-native"})
    first = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "delayed first input"}],
        stable_id="a" * 32,
        background_titles_enabled=False,
    )
    second = pending_inputs.record(
        conv.id,
        [{"type": "input_text", "text": "next input"}],
        stable_id="b" * 32,
        background_titles_enabled=False,
    )
    try:
        if steering:
            ctx._push_injection(request.model_copy(update={"injection_id": "inj_pending"}))
            watcher = asyncio.create_task(adapter._watch_injections(ctx, executor))
            try:
                consumed = await asyncio.wait_for(ctx._event_queue.get(), timeout=5)
                assert isinstance(consumed, InjectionConsumedEvent)
                assert consumed.injection_id == "inj_pending"
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        else:
            with pytest.raises(RuntimeError) as caught:
                await adapter.run_turn(request, ctx)
            assert caught.value.code == "hermes_delivery_unconfirmed"
            assert caught.value.undelivered is False
            assert adapter._executor is executor
        assert sent == ["delayed first input"]
        assert interrupted == []
        assert [p["pending_id"] for p in pending_inputs.snapshot_for(conv.id)] == [first, second]
        if not steering:
            for index, text in enumerate(["delayed first input", "next input"]):
                await _persist_external_conversation_item(
                    conv.id,
                    conv,
                    SessionEventInput(
                        type="external_conversation_item",
                        data={
                            "item_type": "message",
                            "item_data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": text}],
                            },
                            "source_id": f"hermes:late-row:{index}",
                        },
                    ),
                    store,
                )
                assert [p["pending_id"] for p in pending_inputs.snapshot_for(conv.id)] == (
                    [second] if index == 0 else []
                )
            saved = store.list_items(conv.id).data
            expected_ids = [
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"omnigent-external-item:{conv.id}:hermes:late-row:{index}"
                ).hex
                for index in range(2)
            ]
            assert [item.id for item in saved] == expected_ids
            consumed = [
                call.args[1]["data"]
                for call in publish.call_args_list
                if call.args[1].get("type") == "session.input.consumed"
            ]
            assert [(event["item_id"], event["cleared_pending_id"]) for event in consumed] == [
                (expected_ids[0], first),
                (expected_ids[1], second),
            ]
            assert [item.data.content[0]["text"] for item in saved] == [
                "delayed first input",
                "next input",
            ]
        await executor.interrupt_session("main")
        assert interrupted == [True], "explicit user Stop still owns interruption"
    finally:
        pending_inputs.resolve(conv.id, first)
        pending_inputs.resolve(conv.id, second)


async def test_run_turn_marks_readiness_failure_definitely_unsent(tmp_path: Path, monkeypatch):
    def fail(*_a, **_k):
        raise hne.HermesInputNotSent("no message was sent")

    monkeypatch.setattr(hne, "inject_user_message", fail)
    executor = hne.HermesNativeExecutor(tmp_path)
    events = [
        event async for event in executor.run_turn([{"role": "user", "content": "hello"}], [], "")
    ]
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.undelivered is True
    assert error.code == "hermes_input_not_sent"
    assert error.preserve_session is True
    assert await executor.enqueue_session_message("main", "hello") is False
