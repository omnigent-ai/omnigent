"""Tests for conversation-aware runner routing."""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter, runner_dispatch_harness
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.spec import AgentSpec, ExecutorSpec, LLMConfig


class _FakeWebSocket:
    """Minimal WebSocket used only to register runner sessions."""

    async def send_text(self, data: str) -> None:
        """
        Accept a send call from the registry.

        :param data: Encoded tunnel frame.
        :returns: None.
        """
        del data

    async def receive_text(self) -> str:
        """
        Return no frames.

        :returns: Empty frame string.
        """
        return ""


class _ConversationStore:
    """Small in-memory conversation store for runner routing tests."""

    def __init__(self, conversations: dict[str, Conversation]) -> None:
        """
        Create the store.

        :param conversations: Conversations keyed by id.
        """
        self._conversations = conversations

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return a conversation by id.

        :param conversation_id: Conversation id, e.g.
            ``"conv_test"``.
        :returns: The conversation or ``None``.
        """
        return self._conversations.get(conversation_id)


def _conversation(
    conversation_id: str = "conv_test",
    *,
    runner_id: str | None = None,
) -> Conversation:
    """
    Create a real conversation entity.

    :param conversation_id: Conversation id.
    :param runner_id: Optional pinned runner id.
    :returns: A :class:`Conversation`.
    """
    return Conversation(
        id=conversation_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=conversation_id,
        runner_id=runner_id,
    )


def _hello(*, harnesses: list[str]) -> HelloFrame:
    """
    Build a runner hello frame.

    :param harnesses: Harness kinds advertised by the runner.
    :returns: A :class:`HelloFrame`.
    """
    return HelloFrame(
        runner_version="0.1.0-test",
        frame_protocol_version=1,
        harnesses=harnesses,
        envs=["os_sandbox"],
    )


def _assert_omnigent_error(
    excinfo: pytest.ExceptionInfo[OmnigentError],
    *,
    code: str,
) -> None:
    """
    Assert a structured Omnigent error code.

    :param excinfo: Captured pytest exception info.
    :param code: Expected :class:`ErrorCode` value.
    :returns: None.
    """
    assert excinfo.value.code == code


def _agent_spec(
    *,
    executor: ExecutorSpec,
    llm: LLMConfig | None = None,
) -> AgentSpec:
    """
    Build a minimal real agent spec for routing tests.

    Syncs ``llm.model`` into ``executor.model`` to match parser
    consolidation behavior.

    :param executor: Executor block under test.
    :param llm: Optional LLM config.
    :returns: Agent spec with real dataclass types.
    """
    if llm is not None and executor.model is None:
        executor.model = llm.model
    return AgentSpec(
        spec_version=1,
        name="routing-test-agent",
        executor=executor,
        llm=llm,
    )


def test_runner_dispatch_harness_reads_explicit_harness() -> None:
    """Explicit harness-backed specs dispatch through the runner."""
    spec = _agent_spec(
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex"},
        ),
    )

    assert runner_dispatch_harness(spec) == "codex"


def test_runner_dispatch_harness_ignores_unmapped_harness() -> None:
    """Specs with a harness not in the runner module table return None."""
    spec = _agent_spec(
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "open-responses"},
        ),
    )

    assert runner_dispatch_harness(spec) is None


@pytest.mark.asyncio
async def test_runner_router_requires_existing_runner_binding() -> None:
    """Dispatch fails when a conversation has not been PATCH-bound."""
    registry = TunnelRegistry()
    registry.register("runner_one", _FakeWebSocket(), _hello(harnesses=["codex"]))
    conversation = _conversation()
    store = _ConversationStore({"conv_test": conversation})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        with pytest.raises(OmnigentError) as excinfo:
            router.client_for_conversation(conversation_id="conv_test", harness="codex")

        _assert_omnigent_error(excinfo, code=ErrorCode.CONFLICT)
        assert conversation.runner_id is None
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_router_requires_pinned_runner_to_be_online() -> None:
    """A pinned offline runner fails instead of silently rerouting."""
    registry = TunnelRegistry()
    registry.register("runner_other", _FakeWebSocket(), _hello(harnesses=["codex"]))
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_missing")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        with pytest.raises(OmnigentError) as excinfo:
            router.client_for_conversation(conversation_id="conv_test", harness="codex")

        _assert_omnigent_error(excinfo, code=ErrorCode.RUNNER_UNAVAILABLE)
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_router_uses_pinned_runner_when_multiple_online() -> None:
    """A pinned conversation keeps hard affinity with multiple runners online."""
    registry = TunnelRegistry()
    registry.register("runner_one", _FakeWebSocket(), _hello(harnesses=["codex"]))
    registry.register("runner_two", _FakeWebSocket(), _hello(harnesses=["codex"]))
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_two")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        routed = router.client_for_conversation(conversation_id="conv_test", harness="codex")

        assert routed.runner_id == "runner_two"
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_router_fails_when_no_runner_supports_harness() -> None:
    """A harness capability mismatch fails before dispatching."""
    registry = TunnelRegistry()
    registry.register("runner_one", _FakeWebSocket(), _hello(harnesses=["claude-sdk"]))
    store = _ConversationStore({"conv_test": _conversation(runner_id="runner_one")})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        with pytest.raises(OmnigentError) as excinfo:
            router.client_for_conversation(conversation_id="conv_test", harness="codex")

        _assert_omnigent_error(excinfo, code=ErrorCode.RUNNER_CAPABILITY_MISMATCH)
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_router_resources_require_existing_runner_binding() -> None:
    """Resource access fails instead of lazily pinning an unbound session."""
    registry = TunnelRegistry()
    registry.register("runner_one", _FakeWebSocket(), _hello(harnesses=["codex"]))
    conversation = _conversation()
    store = _ConversationStore({"conv_test": conversation})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        with pytest.raises(OmnigentError) as excinfo:
            router.client_for_session_resources("conv_test")

        _assert_omnigent_error(excinfo, code=ErrorCode.CONFLICT)
        assert conversation.runner_id is None
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_router_existing_conversation_returns_none_when_unpinned() -> None:
    """Non-dispatch routes can distinguish unpinned conversations."""
    registry = TunnelRegistry()
    store = _ConversationStore({"conv_test": _conversation()})
    router = RunnerRouter(registry=registry, conversation_store=store)  # type: ignore[arg-type]
    try:
        assert router.client_for_existing_conversation("conv_test") is None
    finally:
        await router.aclose()
