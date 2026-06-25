"""Unit tests for omnigent.runtime.compaction."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from omnigent.entities import (
    CompactionData,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.llms.context_window import resolve_effective_context_window
from omnigent.llms.errors import RetryableLLMError
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runtime.compaction import (
    _BINARY_CONTENT_CLEARED,
    _TOOL_RESULT_CLEARED,
    _pair_aware_drop_count,
    _truncate_oldest,
    compact,
    compaction_to_history_items,
    count_tokens,
    summarize_history,
)
from omnigent.spec.types import CompactionConfig

# ---------------------------------------------------------------------------
# LLM client stubs
# ---------------------------------------------------------------------------


class _RaisesIfCalled:
    """
    LLM client stub that fails the test if ``responses.create()`` is ever
    called.

    Use this for ``compact()`` calls where Layer 2 must NOT fire. If the
    production code unexpectedly reaches ``summarize_history``, the
    ``AssertionError`` surfaces immediately rather than silently succeeding
    via a ``MagicMock``.
    """

    class responses:
        """Namespace mirroring the real client's ``responses`` attribute."""

        @staticmethod
        async def create(**kwargs: Any) -> None:
            """
            Raise if called — Layer 2 must not have fired.

            :param kwargs: Forwarded kwargs from the real API call.
            :raises AssertionError: Always.
            """
            raise AssertionError(
                "llm_client.responses.create() was called unexpectedly. "
                "Layer 2 must not fire in this test — check that count_tokens "
                "is mocked below budget or that summarize_history is patched."
            )


class _ReturnsTextClient:
    """
    LLM client stub that returns a real ``Response`` containing a fixed text.

    Use this for ``summarize_history`` tests where a real LLM response is
    needed but the test must not hit the network.

    :param text: The assistant text the stub will return, e.g.
        ``"Summary of earlier conversation context."``.
    :param model: The model name to embed in the returned ``Response``, e.g.
        ``"openai/gpt-4o"``.
    """

    def __init__(self, text: str, model: str = "test-model") -> None:
        self._text = text
        self._model = model
        self.call_count = 0

    class _Responses:
        """
        Inner namespace mirroring ``client.responses``.

        :param outer: The enclosing ``_ReturnsTextClient`` instance.
        """

        def __init__(self, outer: _ReturnsTextClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Response:
            """
            Return a real ``Response`` with the configured text.

            :param kwargs: Forwarded kwargs from the real API call.
            :returns: A ``Response`` wrapping the configured text.
            """
            self._outer.call_count += 1
            return Response(
                output=[MessageOutput(content=[OutputText(text=self._outer._text)])],
                model=self._outer._model,
            )

    @property
    def responses(self) -> _ReturnsTextClient._Responses:
        """
        Return the ``responses`` namespace for this stub client.

        :returns: The ``_Responses`` inner instance.
        """
        return self._Responses(self)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_conv_item(
    item_id: str,
    item_type: str,
    data: Any,
    response_id: str = "resp_001",
) -> ConversationItem:
    """
    Build a ConversationItem for testing.

    :param item_id: Unique identifier for the item.
    :param item_type: Type string, e.g. "message", "function_call".
    :param data: The item payload (MessageData, FunctionCallData, etc.).
    :param response_id: Response/task identifier to associate with the item.
    """
    return ConversationItem(
        id=item_id,
        type=item_type,
        status="completed",
        response_id=response_id,
        created_at=1000,
        data=data,
    )


def _user_msg(item_id: str, text: str = "User message") -> ConversationItem:
    """
    Build a user-role ConversationItem with a single input_text block.

    :param item_id: Unique identifier for the item.
    :param text: Text content of the user message.
    """
    return _make_conv_item(
        item_id,
        "message",
        MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _assistant_msg(item_id: str, text: str = "Assistant response") -> ConversationItem:
    """
    Build an assistant-role ConversationItem with a single output_text block.

    :param item_id: Unique identifier for the item.
    :param text: Text content of the assistant response.
    """
    return _make_conv_item(
        item_id,
        "message",
        MessageData(
            role="assistant",
            content=[{"type": "output_text", "text": text}],
            agent="test-model",
        ),
    )


def _fc_item(item_id: str, call_id: str = "call_abc") -> ConversationItem:
    """
    Build a function_call ConversationItem.

    :param item_id: Unique identifier for the item.
    :param call_id: Tool call identifier, e.g. "call_abc".
    """
    return _make_conv_item(
        item_id,
        "function_call",
        FunctionCallData(
            agent="test-model",
            name="my_tool",
            arguments="{}",
            call_id=call_id,
        ),
    )


def _fco_item(
    item_id: str,
    call_id: str = "call_abc",
    output: str = "tool result",
) -> ConversationItem:
    """
    Build a function_call_output ConversationItem.

    :param item_id: Unique identifier for the item.
    :param call_id: Tool call identifier matching the originating function_call.
    :param output: The tool output string.
    """
    return _make_conv_item(
        item_id,
        "function_call_output",
        FunctionCallOutputData(call_id=call_id, output=output),
    )


def _user_msg_dict(text: str = "User message") -> dict[str, Any]:
    """
    Build a user-role message dict for the messages list.

    :param text: Text content of the user message.
    """
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _assistant_msg_dict(text: str = "Assistant response") -> dict[str, Any]:
    """
    Build an assistant-role message dict for the messages list.

    :param text: Text content of the assistant response.
    """
    return {"role": "assistant", "content": [{"type": "output_text", "text": text}]}


def _fc_dict(call_id: str = "call_abc", name: str = "my_tool") -> dict[str, Any]:
    """
    Build a function_call dict for the messages list.

    :param call_id: Tool call identifier.
    :param name: Name of the tool being called.
    """
    return {"type": "function_call", "id": call_id, "name": name, "arguments": "{}"}


def _fco_dict(call_id: str = "call_abc", output: str = "tool result") -> dict[str, Any]:
    """
    Build a function_call_output dict for the messages list.

    :param call_id: Tool call identifier matching the originating function_call.
    :param output: The tool output string.
    """
    return {"type": "function_call_output", "call_id": call_id, "output": output}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_compaction_under_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 1 always runs but returns early if token count is within budget."""
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)
    messages = [_user_msg_dict("hi"), _assistant_msg_dict("hello")]
    history = [_user_msg("msg_001", "hi"), _assistant_msg("msg_002", "hello")]

    result = await compact(
        messages,
        history,
        config=None,
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: Layer 2 must not fire (budget met after Layer 1).
        # If summarize_history() is unexpectedly called, the test fails immediately.
        llm_client=_RaisesIfCalled(),
    )

    # Layer 1 always applies clearing, but since budget is met, returns early.
    # summary_metadata=None proves Layer 2 (summarization) never fired.
    assert result.summary_metadata is None
    # Messages content preserved — no tool result bodies were replaced.
    assert result.messages[0]["content"][0]["text"] == "hi"
    assert result.messages[1]["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_layer1_clears_tool_results_outside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Layer 1 replaces function_call_output bodies outside the recent window
    with _TOOL_RESULT_CLEARED, while preserving bodies inside the window.

    ``recent_window=2`` counting backward through [u3,fc3,fco3,a3] and [u2,fc2,fco2,a2]:
    - i=11: a3 → groups=1; i=9: fc3 → groups=2 ≥ 2 → boundary=9.
    - Items 0..8 outside window (eligible for clearing).
    - Items 9..11 inside window (protected: fc3, fco3, a3).
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)

    history = [
        _user_msg("msg_u1", "iter1"),
        _fc_item("msg_fc1", "c1"),
        _fco_item("msg_fco1", "c1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2", "iter2"),
        _fc_item("msg_fc2", "c2"),
        _fco_item("msg_fco2", "c2"),
        _assistant_msg("msg_a2"),
        _user_msg("msg_u3", "iter3"),
        _fc_item("msg_fc3", "c3"),
        _fco_item("msg_fco3", "c3"),
        _assistant_msg("msg_a3"),
    ]
    messages = [
        _user_msg_dict("iter1"),
        _fc_dict("c1"),
        _fco_dict("c1", "tool result iter1"),
        _assistant_msg_dict(),
        _user_msg_dict("iter2"),
        _fc_dict("c2"),
        _fco_dict("c2", "tool result iter2"),
        _assistant_msg_dict(),
        _user_msg_dict("iter3"),
        _fc_dict("c3"),
        _fco_dict("c3", "tool result iter3"),
        _assistant_msg_dict(),
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=2),
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: token count is within budget so Layer 2 must not
        # fire. Fails loudly if summarize_history() is unexpectedly reached.
        llm_client=_RaisesIfCalled(),
    )

    # fco at index 2 (iter1, outside window) must be cleared.
    assert result.messages[2]["output"] == _TOOL_RESULT_CLEARED, (
        f"Expected iter1 tool result to be cleared (outside window), "
        f"got: {result.messages[2]['output']!r}"
    )
    # fco at index 6 (iter2, outside window) must be cleared.
    assert result.messages[6]["output"] == _TOOL_RESULT_CLEARED, (
        f"Expected iter2 tool result to be cleared (outside window), "
        f"got: {result.messages[6]['output']!r}"
    )
    # fco at index 10 (iter3 — inside window, boundary=9 so index 10 ≥ 9) must be preserved.
    assert result.messages[10]["output"] == "tool result iter3", (
        f"Expected iter3 tool result to be preserved (inside window, boundary=9), "
        f"got: {result.messages[10]['output']!r}"
    )
    # summary_metadata=None confirms only Layer 1 fired (Layer 2 not triggered).
    assert result.summary_metadata is None


@pytest.mark.asyncio
async def test_layer1_never_touches_user_message_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Layer 1 (tool result clearing) must never modify user message text content,
    even for messages outside the recent window.
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)

    history = [
        _user_msg("msg_u1", "Important user text outside window"),
        _fc_item("msg_fc1", "c1"),
        _fco_item("msg_fco1", "c1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2", "Another user message inside window"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict("Important user text outside window"),
        _fc_dict("c1"),
        _fco_dict("c1", "tool output"),
        _assistant_msg_dict(),
        _user_msg_dict("Another user message inside window"),
        _assistant_msg_dict(),
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: budget is met after Layer 1 so Layer 2 must not
        # fire. Fails loudly if summarize_history() is unexpectedly reached.
        llm_client=_RaisesIfCalled(),
    )

    # User text at index 0 (outside window) must be preserved.
    # Failure here means Layer 1 modified non-tool-result content.
    assert result.messages[0]["content"][0]["text"] == "Important user text outside window"
    # User text at index 4 (inside window) must also be preserved.
    assert result.messages[4]["content"][0]["text"] == "Another user message inside window"


@pytest.mark.asyncio
async def test_layer1_clears_binary_content_and_preserves_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Layer 1 clears image/file block data outside the recent window,
    preserves file_id, and leaves text blocks in the same message untouched.
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)

    # User message with image block (outside window) + text block
    image_msg = {
        "role": "user",
        "content": [
            {"type": "image", "data": "base64IMAGEDATA==", "file_id": "file_abc123"},
            {"type": "text", "text": "Please describe this image"},
        ],
    }
    history = [
        _user_msg("msg_u1", "user with image"),
        _assistant_msg("msg_a1"),  # boundary (recent_window=1)
    ]
    messages = [image_msg, _assistant_msg_dict()]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: budget is met after Layer 1 so Layer 2 must not
        # fire. Fails loudly if summarize_history() is unexpectedly reached.
        llm_client=_RaisesIfCalled(),
    )

    image_block = result.messages[0]["content"][0]
    text_block = result.messages[0]["content"][1]

    # Image data must be cleared — the binary payload was replaced.
    assert image_block["data"] == _BINARY_CONTENT_CLEARED, (
        f"Expected image data to be cleared, got: {image_block['data']!r}"
    )
    # file_id must be preserved so the agent can re-fetch the image.
    assert image_block["file_id"] == "file_abc123", (
        f"Expected file_id 'file_abc123' preserved, got: {image_block['file_id']!r}"
    )
    # Text block in the same message must be untouched.
    assert text_block["text"] == "Please describe this image"


@pytest.mark.asyncio
async def test_layer1_binary_content_inside_window_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary content inside the recent window must not be cleared by Layer 1."""
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)

    image_msg_outside = {
        "role": "user",
        "content": [{"type": "image", "data": "OLD_DATA==", "file_id": "file_old"}],
    }
    image_msg_inside = {
        "role": "user",
        "content": [{"type": "image", "data": "NEW_DATA==", "file_id": "file_new"}],
    }
    # With recent_window=2 and history [u1, a1, u2, a2]:
    # i=3: a2 → groups=1; i=1: a1 → groups=2 ≥ 2 → boundary=1.
    # Items 0 outside window (image_msg_outside, messages[0]).
    # Items 1..3 inside window (a1, image_msg_inside at msg index 2, a2).
    history = [
        _user_msg("msg_u1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [image_msg_outside, _assistant_msg_dict(), image_msg_inside, _assistant_msg_dict()]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=2),
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: budget is met after Layer 1 so Layer 2 must not fire.
        llm_client=_RaisesIfCalled(),
    )

    # The image OUTSIDE the window (index 0 < boundary=1) should be cleared.
    assert result.messages[0]["content"][0]["data"] == _BINARY_CONTENT_CLEARED
    # The image INSIDE the window (index 2 ≥ boundary=1) must be untouched.
    assert result.messages[2]["content"][0]["data"] == "NEW_DATA==", (
        "Image inside recent window must not be cleared by Layer 1."
    )


@pytest.mark.parametrize(
    ("recent_window", "outside_fco_idx", "protected_fco_idx"),
    [
        # recent_window=2: boundary at index 17 (fc17). fco18 protected; fco14 outside.
        (2, 14, 18),
        # recent_window=3: boundary at index 15 (a15). fco18 protected; fco10 outside.
        (3, 10, 18),
        # recent_window=4: boundary at index 13 (fc13). fco18 protected; fco6 outside.
        (4, 6, 18),
    ],
    ids=["window-2", "window-3", "window-4"],
)
@pytest.mark.asyncio
async def test_recent_window_boundary_parametrized(
    monkeypatch: pytest.MonkeyPatch,
    recent_window: int,
    outside_fco_idx: int,
    protected_fco_idx: int,
) -> None:
    """
    Items inside the recent window must never be modified;
    items outside must have their tool result bodies cleared.
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 50)

    history = []
    messages = []
    for i in range(5):
        call_id = f"c{i}"
        history.extend(
            [
                _user_msg(f"msg_u{i}"),
                _fc_item(f"msg_fc{i}", call_id),
                _fco_item(f"msg_fco{i}", call_id, f"output_iter_{i}"),
                _assistant_msg(f"msg_a{i}"),
            ]
        )
        messages.extend(
            [
                _user_msg_dict(),
                _fc_dict(call_id),
                _fco_dict(call_id, f"output_iter_{i}"),
                _assistant_msg_dict(),
            ]
        )

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=recent_window),
        context_window=100000,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # _RaisesIfCalled: count_tokens is mocked below budget, so Layer 2
        # must not fire. Fails loudly if summarize_history() is called.
        llm_client=_RaisesIfCalled(),
    )

    outside_output = result.messages[outside_fco_idx]["output"]
    protected_output = result.messages[protected_fco_idx]["output"]

    # Tool result outside the recent window must be cleared.
    assert outside_output == _TOOL_RESULT_CLEARED, (
        f"fco at index {outside_fco_idx} should be cleared (outside window={recent_window}), "
        f"got: {outside_output!r}"
    )
    # Tool result inside the recent window must be preserved.
    assert protected_output != _TOOL_RESULT_CLEARED, (
        f"fco at index {protected_fco_idx} should be preserved (inside window={recent_window}), "
        f"but was cleared"
    )


@pytest.mark.asyncio
async def test_layer2_triggers_when_layer1_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When Layer 1 alone is insufficient (token count still above budget),
    Layer 2 (LLM summarization) is triggered.
    """
    call_counts = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        """
        Return above-budget on the first call to force Layer 2, then below-budget
        for all subsequent calls so Layer 2 can succeed.
        """
        call_counts[0] += 1
        # First call (after Layer 1): above budget → trigger Layer 2
        # Second call (inside _run_layer2): check if to_summarize too large
        # Third call (after Layer 2): summary + recent fits budget
        if call_counts[0] == 1:
            return 10001  # above budget=10000 → Layer 2 needed
        return 50  # all subsequent calls: below budget

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _stub_summarize(
        msgs: list[dict[str, Any]],
        llm_client: Any,
        model: str,
        connection: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a fixed summary result."""
        return {
            "text": "Summary of earlier conversation",
            "token_count": 50,
        }

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _stub_summarize,
    )

    # 2 iterations; recent_window=1 → boundary at index 7 (last assistant)
    history = [
        _user_msg("msg_u1"),
        _fc_item("msg_fc1", "c1"),
        _fco_item("msg_fco1", "c1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _fc_item("msg_fc2", "c2"),
        _fco_item("msg_fco2", "c2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict(),
        _fc_dict("c1"),
        _fco_dict("c1"),
        _assistant_msg_dict(),
        _user_msg_dict(),
        _fc_dict("c2"),
        _fco_dict("c2"),
        _assistant_msg_dict(),
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,  # budget = int(12500*0.8) = 10000
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # summarize_history is monkeypatched above so llm_client is never used.
        # _RaisesIfCalled still catches any accidental bypass of the patch.
        llm_client=_RaisesIfCalled(),
    )

    # summary_metadata being set proves Layer 2 fired successfully.
    assert result.summary_metadata is not None, (
        "Layer 2 should have triggered and set summary_metadata, "
        "but it is None — check that mock count_tokens returns > budget on first call."
    )
    # The summary text must match what summarize_history returned.
    assert result.summary_metadata.text == "Summary of earlier conversation"
    # last_item_id must point to a real history item before the boundary.
    # boundary=7 (last assistant) → last summarized item = msg_fco2 at index 6 (non-synthetic).
    # Actually: _find_last_summarized_item_id looks for last non-synthetic item before boundary.
    # With recent_window=1, boundary=7, last item before boundary is msg_fco2 at index 6.
    # Wait - boundary=7 means items 7+ are protected. Items 0..6 are summarized.
    # _find_last_summarized_item_id(history, boundary=7) → history[6] = msg_fco2.
    assert result.summary_metadata.last_item_id == "msg_fco2", (
        f"last_item_id should point to the last item before the boundary, "
        f"got: {result.summary_metadata.last_item_id!r}"
    )
    # The compacted messages should start with the synthetic summary pair
    # (user + assistant messages from _summary_to_messages).
    assert result.messages[0]["role"] == "user"
    assert "automatically generated summary" in result.messages[0]["content"]
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[1]["content"] == "Summary of earlier conversation"


@pytest.mark.asyncio
async def test_layer2_failure_falls_back_to_layer3(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When Layer 2 summarization fails, compact() falls back to Layer 3
    (truncation) without raising. summary_metadata is None.
    """
    # First 2 calls above budget (trigger Layer 2); subsequent calls below budget
    # so Layer 3 stops truncating after one pass (not emptying the list).
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        """
        Return above-budget on the first 2 calls to trigger Layer 2,
        then below-budget so Layer 3 terminates with remaining messages.
        """
        call_idx[0] += 1
        return 10001 if call_idx[0] <= 2 else 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _raise_retryable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Raise RetryableLLMError to simulate an unavailable LLM."""
        raise RetryableLLMError("LLM unavailable", code="503")

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _raise_retryable,
    )

    history = [
        _user_msg("msg_u1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict("first"),
        _assistant_msg_dict(),
        _user_msg_dict("second"),
        _assistant_msg_dict(),
    ]

    # Must not raise even though summarize_history fails.
    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_001",
        # summarize_history is monkeypatched to raise before reaching llm_client.
        # _RaisesIfCalled catches any accidental bypass of the monkeypatch.
        llm_client=_RaisesIfCalled(),
    )

    # summary_metadata=None proves Layer 2 failed (not persisted).
    assert result.summary_metadata is None, (
        "summary_metadata must be None when Layer 2 summarization fails — "
        "it is only set on successful summarization."
    )
    # Some messages must have been returned (Layer 3 truncated, not emptied).
    assert len(result.messages) > 0


@pytest.mark.asyncio
async def test_layer2_401_logs_auth_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A 401 during Layer 2 should name the auth/config problem before
    falling back to Layer 3.
    """
    call_idx = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        """
        Trigger Layer 2, then let Layer 3 finish after one truncation pass.
        """
        call_idx[0] += 1
        return 10001 if call_idx[0] <= 2 else 50

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", mock_count_tokens)

    async def _raise_unauthorized(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Raise an LLM error shaped like a provider HTTP 401."""
        raise RetryableLLMError("Unauthorized", code="401")

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _raise_unauthorized,
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runtime.compaction"):
        result = await compact(
            [_user_msg_dict("first"), _assistant_msg_dict()],
            [_user_msg("msg_u1"), _assistant_msg("msg_a1")],
            config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
            context_window=12500,
            system_token_budget=0,
            model="openai/gpt-4o",
            task_id="task_401",
            llm_client=_RaisesIfCalled(),
        )

    assert result.summary_metadata is None
    assert "HTTP 401 Unauthorized" in caplog.text
    assert "configured LLM credentials/base URL" in caplog.text
    assert "task_401" in caplog.text


@pytest.mark.asyncio
async def test_summarize_history_returns_text_and_token_count() -> None:
    """summarize_history calls the LLM and returns text + token_count > 0."""
    summary_text = "Summary of earlier conversation context."
    stub_llm = _ReturnsTextClient(text=summary_text, model="openai/gpt-4o")

    messages = [{"role": "user", "content": "prior conversation"}]
    result = await summarize_history(messages, stub_llm, "openai/gpt-4o")

    # The "text" field must match what the LLM returned.
    assert result["text"] == summary_text, (
        f"Expected summary text from LLM response, got: {result['text']!r}"
    )
    # token_count must be positive — proves count_tokens ran on the text.
    assert result["token_count"] > 0, (
        "token_count must be > 0; failure means count_tokens wasn't called or returned 0."
    )
    # The LLM must have been called exactly once.
    assert stub_llm.call_count == 1, (
        f"Expected 1 LLM call, got {stub_llm.call_count}. "
        "Failure means summarize_history called the LLM more than once or not at all."
    )


@pytest.mark.asyncio
async def test_summarize_history_recursive_prompt_includes_continuation_prefix() -> None:
    """
    When history starts with a prior summary, the summarization prompt
    includes a 'Incorporate it' continuation instruction.
    """
    # Prior summary header that triggers recursive detection
    prior_summary_header = (
        "[This is an automatically generated summary of the prior conversation "
        "context. The original messages are available but not included in this "
        "prompt for brevity.]\n\n"
        "Please provide a summary of our conversation so far."
    )
    messages = [
        {"role": "user", "content": prior_summary_header},
        {"role": "assistant", "content": "Earlier we discussed X and Y."},
        {"role": "user", "content": "Now let's continue with Z."},
    ]

    captured_instructions: list[str] = []
    mock_resp = Response(
        output=[MessageOutput(content=[OutputText(text="Combined summary.")])],
        model="openai/gpt-4o",
    )

    class _CapturingClient:
        """LLM client stub that captures ``instructions`` from each call."""

        class responses:
            """Namespace mirroring the real client's ``responses`` attribute."""

            @staticmethod
            async def create(**kwargs: Any) -> Response:
                """Capture the instructions kwarg and return the mock response."""
                captured_instructions.append(kwargs.get("instructions", ""))
                return mock_resp

    result = await summarize_history(messages, _CapturingClient(), "openai/gpt-4o")

    assert len(captured_instructions) == 1
    # The continuation prefix must be present when history starts with a prior summary.
    assert "Incorporate it into your new summary" in captured_instructions[0], (
        "Recursive summarization prompt must include the 'Incorporate it' instruction; "
        "failure means _build_summarization_prompt did not detect the prior summary header."
    )
    assert result["text"] == "Combined summary."


def test_compaction_to_history_items_produces_valid_pair() -> None:
    """
    compaction_to_history_items() produces a user+assistant synthetic pair
    for inclusion at the start of conversation history.
    """
    compaction_item = ConversationItem(
        id="cmp_abc123",
        type="compaction",
        status="completed",
        response_id="task_001",
        created_at=1000,
        data=CompactionData(
            summary="The user asked to analyze the dataset. The agent loaded data.csv.",
            last_item_id="msg_xyz999",
            model="openai/gpt-4o",
            token_count=42,
        ),
    )

    result = compaction_to_history_items(compaction_item)

    # Must return exactly 2 items: synthetic user + assistant.
    assert len(result) == 2, (
        f"Expected exactly 2 items (user + assistant), got {len(result)}. "
        "Failure means compaction_to_history_items changed its output shape."
    )
    user_item = result[0]
    assistant_item = result[1]

    # Both items must be message type for history processing.
    assert user_item.type == "message"
    assert assistant_item.type == "message"

    # User item must have role=user.
    assert isinstance(user_item.data, MessageData)
    assert user_item.data.role == "user"

    # User content must contain the summary marker prefix so the LLM
    # understands this is synthetic context, not a real prior message.
    user_text = user_item.data.content[0]["text"]
    assert "[This is an automatically generated summary" in user_text, (
        "User content must contain the summary marker prefix — "
        "failure means the synthetic header was changed or removed."
    )

    # Assistant item must have the summary text verbatim.
    assert isinstance(assistant_item.data, MessageData)
    assert assistant_item.data.role == "assistant"
    assistant_text = assistant_item.data.content[0]["text"]
    assert assistant_text == "The user asked to analyze the dataset. The agent loaded data.csv.", (
        f"Assistant content must equal the CompactionData.summary, got: {assistant_text!r}"
    )

    # IDs must be derived from the compaction item ID.
    assert user_item.id == "cmp_abc123_user"
    assert assistant_item.id == "cmp_abc123_assistant"


def test_count_tokens_returns_positive_integer() -> None:
    """count_tokens returns a positive integer for non-empty messages."""
    messages = [{"role": "user", "content": "Hello world, this is a test message."}]
    result = count_tokens(messages, "openai/gpt-4o")
    # Must be a positive integer; failure means tiktoken encoding failed.
    assert isinstance(result, int)
    assert result > 0


def test_count_tokens_unknown_model_falls_back() -> None:
    """Unknown model falls back to cl100k_base encoding without raising."""
    messages = [{"role": "user", "content": "test"}]
    # Should not raise even for completely unknown model names.
    result = count_tokens(messages, "unknown/totally-fake-model-xyz")
    assert result > 0


async def test_layer3_truncation_preserves_tool_call_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Layer 3 truncation drops tool call pairs together — never
    orphans a ``function_call`` without its ``function_call_output``.
    """

    def mock_count_tokens(
        msgs: list[dict[str, Any]],
        model: str,
    ) -> int:
        """
        Simulates shrinking token count as messages are truncated.

        :param msgs: Messages list (length used to simulate shrinking).
        :param model: Model string (unused).
        :returns: Token count proportional to message count.
        """
        # Each message ~ 5000 tokens. Budget is 10000, so need <= 2 msgs.
        return len(msgs) * 5000

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        mock_count_tokens,
    )

    # Layer 2 fails so we fall through to Layer 3.
    async def _raise_layer2(
        msgs: list[dict[str, Any]],
        llm_client: Any,
        model: str,
        connection: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Raise to simulate Layer 2 failure."""
        raise RuntimeError("Simulated Layer 2 failure")

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _raise_layer2,
    )

    # Layout: user, fc+fco pair, assistant, user, assistant
    # 6 messages x 5000 = 30000 > budget 10000
    # Layer 3 must drop from front but keep fc+fco together.
    history = [
        _user_msg("msg_u1"),
        _fc_item("msg_fc1", "c1"),
        _fco_item("msg_fco1", "c1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict(),
        _fc_dict("c1"),
        _fco_dict("c1"),
        _assistant_msg_dict(),
        _user_msg_dict(),
        _assistant_msg_dict(),
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,  # budget = 10000
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_trunc",
        llm_client=_RaisesIfCalled(),
    )

    # After truncation, remaining messages must not have orphaned pairs.
    # An orphaned function_call_output without its function_call is a bug.
    remaining_types = [m.get("type", m.get("role", "unknown")) for m in result.messages]

    for i, msg in enumerate(result.messages):
        if msg.get("type") == "function_call_output":
            # The preceding message must be its matching function_call.
            assert i > 0, "function_call_output at index 0 is orphaned without its function_call."
            prev = result.messages[i - 1]
            assert prev.get("type") == "function_call", (
                f"function_call_output at index {i} is preceded by "
                f"{prev.get('type', prev.get('role'))!r}, not "
                f"'function_call'. The pair was broken by truncation. "
                f"Remaining types: {remaining_types}"
            )
            assert prev.get("call_id") == msg.get("call_id"), (
                f"function_call.call_id={prev.get('call_id')!r} doesn't "
                f"match function_call_output.call_id="
                f"{msg.get('call_id')!r} at index {i}."
            )

    # Must have truncated at least 2 messages (budget fits <= 2).
    # Original had 6 messages x 5000 = 30000 > 10000 budget.
    assert len(result.messages) <= 2, (
        f"Expected <= 2 messages after truncation (budget=10000, "
        f"5000 tokens/msg), got {len(result.messages)}. "
        f"Layer 3 didn't truncate enough."
    )


@pytest.mark.asyncio
async def test_layer2_receives_cleared_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When Layer 2 fires, ``summarize_history`` receives messages
    that already have tool result bodies cleared by Layer 1.

    Captures the messages passed to ``summarize_history`` and
    verifies the tool result body was replaced with the clearing
    marker before summarization.
    """
    call_counts = [0]

    def mock_count_tokens(
        msgs: list[dict[str, Any]],
        model: str,
    ) -> int:
        """
        First call above budget to trigger Layer 2, then below.

        :param msgs: Messages (unused).
        :param model: Model string (unused).
        :returns: Token count.
        """
        call_counts[0] += 1
        if call_counts[0] == 1:
            return 10001
        return 50

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        mock_count_tokens,
    )

    captured_inputs: list[list[dict[str, Any]]] = []

    async def _capturing_summarize(
        msgs: list[dict[str, Any]],
        llm_client: Any,
        model: str,
        connection: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Capture the messages passed to summarize_history.

        :param msgs: The messages to summarize — should have
            cleared tool result bodies.
        :param llm_client: LLM client (unused).
        :param model: Model string (unused).
        :param connection: Connection params (unused).
        :returns: Fake summary result.
        """
        captured_inputs.append(list(msgs))
        return {"text": "Summary", "token_count": 10}

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _capturing_summarize,
    )

    # History with a tool call pair OUTSIDE the recent window.
    history = [
        _user_msg("msg_u1"),
        _fc_item("msg_fc1", "c1"),
        _fco_item("msg_fco1", "c1", output="verbose tool output"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict(),
        _fc_dict("c1"),
        _fco_dict("c1", "verbose tool output"),
        _assistant_msg_dict(),
        _user_msg_dict(),
        _assistant_msg_dict(),
    ]

    await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_layer1_feeds_layer2",
        llm_client=_RaisesIfCalled(),
    )

    # summarize_history must have been called exactly once.
    assert len(captured_inputs) == 1, (
        f"Expected 1 call to summarize_history, got {len(captured_inputs)}."
    )

    # The tool result body in the summarization input must be cleared.
    # Layer 1 runs before Layer 2, so fco at index 2 (outside window)
    # should have its output replaced with the clearing marker.
    summarized = captured_inputs[0]
    fco_in_summary = [m for m in summarized if m.get("type") == "function_call_output"]
    assert len(fco_in_summary) >= 1, (
        "Expected at least 1 function_call_output in summarization input."
    )
    assert fco_in_summary[0]["output"] == _TOOL_RESULT_CLEARED, (
        f"Tool result body should be cleared before reaching "
        f"summarize_history, got: {fco_in_summary[0]['output']!r}. "
        f"If it contains 'verbose tool output', Layer 1 didn't "
        f"clear before passing to Layer 2."
    )


@pytest.mark.asyncio
async def test_layer3_fires_when_summary_plus_recent_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Layer 3 fires as the primary path (not fallback) when Layer 2
    succeeds but the summary + recent messages together still
    exceed the budget.

    This differs from ``test_layer2_failure_falls_back_to_layer3``
    which tests Layer 3 after Layer 2 failure. Here Layer 2
    succeeds but its output is still too large.
    """
    call_counts = [0]

    def mock_count_tokens(
        msgs: list[dict[str, Any]],
        model: str,
    ) -> int:
        """
        Always above budget so Layer 3 must truncate.

        :param msgs: Messages (length-based estimate).
        :param model: Model string (unused).
        :returns: Token count.
        """
        call_counts[0] += 1
        # First call (after Layer 1): above budget → Layer 2 fires
        if call_counts[0] == 1:
            return 10001
        # Second call (inside _run_layer2 size check): below budget
        # so summarization input fits the model
        if call_counts[0] == 2:
            return 50
        # Third call (summary + recent budget check): ABOVE budget
        # → Layer 2 output doesn't fit, fall through to Layer 3
        if call_counts[0] == 3:
            return 10001
        # Layer 3 truncation calls: shrink per message
        return len(msgs) * 3000

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        mock_count_tokens,
    )

    async def _stub_summarize(
        msgs: list[dict[str, Any]],
        llm_client: Any,
        model: str,
        connection: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a large summary that still exceeds budget."""
        return {
            "text": "A very long summary that still exceeds budget",
            "token_count": 9000,
        }

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _stub_summarize,
    )

    history = [
        _user_msg("msg_u1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
        _user_msg("msg_u3"),
        _assistant_msg("msg_a3"),
    ]
    messages = [
        _user_msg_dict("m1"),
        _assistant_msg_dict("a1"),
        _user_msg_dict("m2"),
        _assistant_msg_dict("a2"),
        _user_msg_dict("m3"),
        _assistant_msg_dict("a3"),
    ]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_layer3_primary",
        llm_client=_RaisesIfCalled(),
    )

    # Layer 2 succeeded (summary was produced) but the combined
    # result was too large. Layer 3 must have truncated further.
    # The result should have fewer messages than summary + recent.
    assert len(result.messages) < 6, (
        f"Expected Layer 3 truncation to reduce message count "
        f"below 6, got {len(result.messages)}. "
        f"If 6, Layer 3 didn't fire after Layer 2's output "
        f"exceeded the budget."
    )
    # The first message should be the synthetic summary user
    # message (from Layer 2's output), possibly truncated further.
    assert result.messages[0]["role"] == "user", (
        f"First message should be the summary user message, "
        f"got role={result.messages[0].get('role')!r}."
    )


def test_pair_aware_drop_count_drops_both_when_pair_at_front() -> None:
    """
    When the first two messages are a function_call followed by its
    matching function_call_output, both must be dropped together.

    If only one were dropped, the LLM would see an orphaned
    function_call_output without its parent call (or vice versa).
    """
    messages = [
        {"type": "function_call", "call_id": "c1", "name": "grep", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "result"},
        _user_msg_dict("after the pair"),
    ]
    assert _pair_aware_drop_count(messages) == 2, (
        "Expected 2 (drop both halves of the tool call pair). "
        "If 1, the function_call_output would be orphaned."
    )


def test_pair_aware_drop_count_drops_one_for_non_pair() -> None:
    """
    When the front message is not part of a tool call pair, only
    one item should be dropped.
    """
    messages = [
        _user_msg_dict("hello"),
        _assistant_msg_dict("world"),
    ]
    assert _pair_aware_drop_count(messages) == 1, (
        "Expected 1 for a plain user message at the front."
    )


def test_pair_aware_drop_count_drops_one_for_mismatched_call_ids() -> None:
    """
    A function_call followed by a function_call_output with a
    *different* call_id is not a pair — drop only the first item.
    """
    messages = [
        {"type": "function_call", "call_id": "c1", "name": "grep", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "result"},
    ]
    assert _pair_aware_drop_count(messages) == 1, (
        "Expected 1 — mismatched call_ids means these are not a "
        "pair, so only the first item should be dropped."
    )


def test_pair_aware_drop_count_returns_zero_for_empty() -> None:
    """Empty list returns 0 — nothing to drop."""
    assert _pair_aware_drop_count([]) == 0


def test_truncate_oldest_preserves_tool_call_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    _truncate_oldest drops function_call + function_call_output
    together, never leaving an orphaned half.

    Uses a mock token counter that returns above-budget on the
    first call (triggering one drop) then below-budget so
    truncation stops.
    """
    call_count = [0]

    def mock_count_tokens(msgs: list[dict[str, Any]], model: str) -> int:
        """
        Above budget on first call to trigger one truncation round,
        then below budget so the loop exits.

        :param msgs: Messages list.
        :param model: Model string (unused).
        :returns: Token count.
        """
        call_count[0] += 1
        # First call: above budget. After dropping the pair (2 items),
        # second call: below budget.
        return 10000 if call_count[0] == 1 else 50

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        mock_count_tokens,
    )

    messages = [
        {"type": "function_call", "call_id": "c1", "name": "grep", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "grep result"},
        _user_msg_dict("kept message"),
    ]

    result = _truncate_oldest(messages, budget=100, model="test")

    # The pair (indices 0-1) must be dropped together, leaving
    # only the user message. If the pair were split, we'd see
    # an orphaned function_call_output at index 0.
    assert len(result) == 1, (
        f"Expected 1 message after dropping the tool call pair, "
        f"got {len(result)}. If 2, only one half of the pair was "
        f"dropped (orphaned tool call)."
    )
    assert result[0]["role"] == "user", (
        f"Expected the surviving message to be the user message, "
        f"got type={result[0].get('type')!r} role={result[0].get('role')!r}."
    )


@pytest.mark.asyncio
async def test_compaction_strips_annotations_before_summarization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Layer 1 strips ``annotations`` from ``output_text`` blocks
    before they reach Layer 2 summarization.

    Captures the messages passed to ``summarize_history`` and
    verifies annotations are absent.
    """
    call_counts = [0]

    def mock_count_tokens(
        msgs: list[dict[str, Any]],
        model: str,
    ) -> int:
        """
        First call above budget to trigger Layer 2, then below.

        :param msgs: Messages (unused).
        :param model: Model string (unused).
        :returns: Token count.
        """
        call_counts[0] += 1
        if call_counts[0] == 1:
            return 10001
        return 50

    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        mock_count_tokens,
    )

    captured_inputs: list[list[dict[str, Any]]] = []

    async def _capturing_summarize(
        msgs: list[dict[str, Any]],
        llm_client: Any,
        model: str,
        connection: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Capture summarization input for assertion.

        :param msgs: Messages to summarize.
        :param llm_client: LLM client (unused).
        :param model: Model string (unused).
        :param connection: Connection params (unused).
        :returns: Fake summary.
        """
        captured_inputs.append(list(msgs))
        return {"text": "Summary", "token_count": 10}

    monkeypatch.setattr(
        "omnigent.runtime.compaction.summarize_history",
        _capturing_summarize,
    )

    # Assistant message with file_citation annotation OUTSIDE recent window.
    annotated_msg = {
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": "Here is the chart:",
                "annotations": [
                    {
                        "type": "file_citation",
                        "file_id": "file_abc123",
                        "filename": "chart.png",
                        "content_type": "image/png",
                    }
                ],
            }
        ],
    }
    history = [
        _user_msg("msg_u1"),
        _assistant_msg("msg_a1"),
        _user_msg("msg_u2"),
        _assistant_msg("msg_a2"),
    ]
    messages = [
        _user_msg_dict(),
        annotated_msg,
        _user_msg_dict(),
        _assistant_msg_dict(),
    ]

    await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=12500,
        system_token_budget=0,
        model="openai/gpt-4o",
        task_id="task_ann_strip",
        llm_client=_RaisesIfCalled(),
    )

    # summarize_history must have been called.
    assert len(captured_inputs) == 1, (
        f"Expected 1 summarize_history call, got {len(captured_inputs)}."
    )

    # The annotated output_text block in the summarization input
    # must NOT have annotations — they should be stripped by Layer 1.
    summarized = captured_inputs[0]
    for msg in summarized:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                assert "annotations" not in block, (
                    f"output_text block still has annotations in "
                    f"summarization input: {block}. Layer 1 should "
                    f"have stripped them."
                )


# ---------------------------------------------------------------------------
# Budget honors the declared/effective context window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_window_keeps_large_fill_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A declared 1M window keeps a Polly-scale fill (~197K) under budget.

    Regression for the runner over-compaction bug: budget is
    ``context_window * trigger_threshold``. With the declared 1M window
    (resolved via resolve_effective_context_window), budget=800K and a 197K
    fill does NOT trigger Layer 2. If the window were the 128K catalog default
    (budget=102400), the same fill would compact — which is the bug.
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 197_000)
    messages = [_user_msg_dict("hi"), _assistant_msg_dict("hello")]
    history = [_user_msg("msg_001", "hi"), _assistant_msg("msg_002", "hello")]

    window = resolve_effective_context_window(1_000_000, "claude-opus-4-8")
    assert window == 1_000_000

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=window,
        system_token_budget=0,
        model="claude-opus-4-8",
        task_id="task_001",
        # 197K <= 0.8 * 1M = 800K → under budget → Layer 2 must NOT fire.
        llm_client=_RaisesIfCalled(),
    )
    assert result.summary_metadata is None


@pytest.mark.asyncio
async def test_default_window_compacts_same_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The same ~197K fill DOES compact against the 128K catalog default.

    Contrast with the test above: this is the pre-fix behavior (budget=102400),
    confirming the window value is what flips compaction on/off.
    """
    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", lambda msgs, model: 197_000)
    messages = [_user_msg_dict("hi"), _assistant_msg_dict("hello")]
    history = [_user_msg("msg_001", "hi"), _assistant_msg("msg_002", "hello")]

    result = await compact(
        messages,
        history,
        config=CompactionConfig(trigger_threshold=0.8, recent_window=1),
        context_window=128_000,
        system_token_budget=0,
        model="claude-opus-4-8",
        task_id="task_001",
        # 197K > 0.8 * 128K = 102400 → over budget → Layer 2 fires.
        llm_client=_ReturnsTextClient("Summary of earlier context."),
    )
    assert result.summary_metadata is not None
