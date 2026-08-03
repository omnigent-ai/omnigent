"""Tests for :class:`DatabricksGenieExecutor` with a fake Responses client.

The executor drives a Genie space in Agent mode over the Genie Responses API.
A small set of fakes mirror the streamed shapes it reads — ``response.created``,
``response.output_item.done`` items, ``response.failed``, ``response.completed``
— so every branch is exercised without the OpenAI SDK, the Databricks SDK, or a
live workspace.

Two node shapes matter and both are covered: parsed models (attribute access)
and raw dicts (key access). Genie's Agent mode is Beta and free to ship fields
and item variants the installed SDK does not model, which it then leaves
unvalidated, so the executor reads either shape deliberately.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from omnigent.inner import databricks_executor
from omnigent.inner.databricks_genie_executor import (
    _MAX_RESULT_ROWS,
    DatabricksGenieExecutor,
    _block_separator,
    _extract_text,
    _latest_user_text,
    _md_cell,
    _output_payload,
    _parse_arguments,
    _reasoning_text,
    _render_table,
)
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.open_responses_sdk import _OPENAI_KEY_PLACEHOLDER


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Fakes mirroring the streamed Responses shapes the executor reads
# ---------------------------------------------------------------------------


@dataclass
class FakeError:
    """Stand-in for the ``error`` object on a failed response."""

    type: str = ""
    code: str = ""
    message: str = ""


@dataclass
class FakeResponseInfo:
    """Stand-in for the ``response`` object carried by lifecycle events."""

    conversation_id: str | None = None
    error: FakeError | None = None
    incomplete_details: Any = None


@dataclass
class FakeEvent:
    """Stand-in for one streamed SSE event."""

    type: str
    item: Any = None
    response: FakeResponseInfo | None = None


@dataclass
class FakeTextPart:
    """``output_text`` content part; ``metadata`` carries Genie's result table."""

    text: str
    type: str = "output_text"
    metadata: Any = None


@dataclass
class FakeMessageItem:
    content: list[Any] = field(default_factory=list)
    type: str = "message"


@dataclass
class FakeReasoningItem:
    content: list[Any] = field(default_factory=list)
    summary: list[Any] = field(default_factory=list)
    type: str = "reasoning"


@dataclass
class FakeFunctionCallItem:
    name: str = "execute_sql"
    arguments: str = "{}"
    call_id: str = "call_1"
    id: str = "fc_1"
    type: str = "function_call"


class FakeResponses:
    """Stands in for ``client.responses``: records kwargs, replays a script.

    ``error`` is public so a test can arm a failure between turns (the 409
    "another turn is in progress" case only makes sense on a second call).
    """

    def __init__(self, events: Any = (), *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.last_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return iter(self.events)


class FakeClient:
    def __init__(self, events: Any = (), *, error: Exception | None = None) -> None:
        self.responses = FakeResponses(events, error=error)


class _Status4xx(Exception):
    """Exception shaped like an OpenAI ``APIStatusError`` (carries a status code)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _created(conversation_id: str | None = None) -> FakeEvent:
    return FakeEvent("response.created", response=FakeResponseInfo(conversation_id))


def _item_done(item: Any) -> FakeEvent:
    return FakeEvent("response.output_item.done", item=item)


def _completed(conversation_id: str | None = None) -> FakeEvent:
    return FakeEvent("response.completed", response=FakeResponseInfo(conversation_id))


def _failed(**error: str) -> FakeEvent:
    return FakeEvent("response.failed", response=FakeResponseInfo(error=FakeError(**error)))


def _incomplete(reason: str | None = None) -> FakeEvent:
    details = {"reason": reason} if reason is not None else None
    return FakeEvent("response.incomplete", response=FakeResponseInfo(incomplete_details=details))


def _error_event(**fields: str) -> dict[str, Any]:
    """A bare ``error`` event — no ``response`` wrapper, fields at the top level."""
    return {"type": "error", **fields}


def _text_item(*parts: FakeTextPart) -> FakeMessageItem:
    return FakeMessageItem(content=list(parts))


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _events(
    executor: DatabricksGenieExecutor, messages: list[dict[str, Any]] | None = None
) -> list[Any]:
    async def _collect() -> list[Any]:
        return [e async for e in executor.run_turn(messages or [_user("q")], [], "SYS")]

    return _run(_collect())


def _texts(events: list[Any]) -> list[str]:
    return [e.text for e in events if isinstance(e, TextChunk)]


def _response(events: list[Any]) -> str | None:
    return next(e.response for e in events if isinstance(e, TurnComplete))


# ---------------------------------------------------------------------------
# I/O matrix: first turn, follow-up turn, request shape
# ---------------------------------------------------------------------------


def test_first_turn_maps_every_item_kind_to_its_event() -> None:
    """Reasoning, SQL, and report items become the matching executor events."""
    client = FakeClient(
        [
            _created("conv-1"),
            _item_done(
                FakeReasoningItem(content=[{"type": "reasoning_text", "text": "Plan it."}])
            ),
            _item_done(FakeFunctionCallItem(arguments='{"query": "SELECT 1"}')),
            _item_done({"type": "function_call_output", "call_id": "call_1", "output": "[[1]]"}),
            _item_done(_text_item(FakeTextPart(text="One row."))),
            _completed(),
        ]
    )
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    events = _events(executor)

    assert [type(e).__name__ for e in events] == [
        "ReasoningChunk",
        "ReasoningChunk",
        "ToolCallRequest",
        "ToolCallComplete",
        "TextChunk",
        "TurnComplete",
    ]
    anchor, reasoning = (e for e in events if isinstance(e, ReasoningChunk))
    assert anchor.delta == ""
    assert anchor.event_type == "reasoning_started"
    assert reasoning.delta == "Plan it."
    assert reasoning.event_type == "reasoning_text"
    assert _response(events) == "One row."


def test_first_turn_request_omits_conversation_id() -> None:
    """With no stored id, the request body carries no ``conversation_id``."""
    client = FakeClient([_created("conv-1"), _completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    _events(executor, [_user("how many sales?")])

    kwargs = client.responses.last_kwargs
    assert kwargs["model"] == "genie-agent"
    assert kwargs["stream"] is True
    assert kwargs["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "how many sales?"}],
        }
    ]
    assert "conversation_id" not in kwargs.get("extra_body", {})


def test_follow_up_turn_sends_the_captured_conversation_id() -> None:
    """The id from ``response.created`` is sent on the next turn's request."""
    client = FakeClient([_created("conv-42"), _completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    _events(executor, [_user("first")])
    _events(executor, [_user("second")])

    assert "conversation_id" not in client.responses.calls[0].get("extra_body", {})
    assert client.responses.calls[1]["extra_body"]["conversation_id"] == "conv-42"


def test_input_carries_only_the_latest_user_message() -> None:
    """Genie owns the history, so prior turns are never replayed into ``input``."""
    client = FakeClient([_created("conv-1"), _completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    _events(
        executor,
        [
            _user("old question"),
            {"role": "assistant", "content": "old answer"},
            _user("new question"),
        ],
    )

    sent = client.responses.last_kwargs["input"]
    assert len(sent) == 1
    assert sent[0]["content"] == [{"type": "input_text", "text": "new question"}]


def test_response_without_conversation_id_leaves_the_next_turn_fresh() -> None:
    """No id in ``response.created`` → the follow-up starts a new conversation."""
    client = FakeClient([_created(None), _completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    _events(executor, [_user("first")])
    _events(executor, [_user("second")])

    assert all("conversation_id" not in c.get("extra_body", {}) for c in client.responses.calls)


def test_conversation_id_is_captured_from_response_completed() -> None:
    """Genie may name the conversation only at the end; the next turn still continues it."""
    client = FakeClient([_created(None), _completed("conv-late")])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)

    _events(executor, [_user("first")])
    _events(executor, [_user("second")])

    assert "conversation_id" not in client.responses.calls[0].get("extra_body", {})
    assert client.responses.calls[1]["extra_body"]["conversation_id"] == "conv-late"


def test_each_reasoning_item_opens_its_own_block() -> None:
    """Genie plans before every SQL call; adjacent items must not run together.

    The ``reasoning_started`` anchor before each item's text is what lets the
    web reducer separate consecutive blocks instead of gluing sentences.
    """
    client = FakeClient(
        [
            _item_done(FakeReasoningItem(content=[{"text": "Check the tables."}])),
            _item_done(FakeReasoningItem(content=[{"text": "Aggregate by region."}])),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert [(e.event_type, e.delta) for e in reasoning] == [
        ("reasoning_started", ""),
        ("reasoning_text", "Check the tables."),
        ("reasoning_started", ""),
        ("reasoning_text", "Aggregate by region."),
    ]


def test_whitespace_only_reasoning_emits_no_chunk() -> None:
    """A formatting-artifact item must not open a visually empty reasoning block."""
    client = FakeClient(
        [
            _item_done(FakeReasoningItem(content=[{"text": "  \n"}])),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TurnComplete"]


def test_enable_viz_is_sent_only_when_enabled() -> None:
    """``enable_viz`` reaches ``extra_body`` as a real bool, and only when on."""
    on = FakeClient([_completed()])
    _events(DatabricksGenieExecutor(space_id="sp", client=on, enable_viz=True))
    assert on.responses.last_kwargs["extra_body"] == {"enable_viz": True}

    off = FakeClient([_completed()])
    _events(DatabricksGenieExecutor(space_id="sp", client=off, enable_viz=False))
    assert "enable_viz" not in off.responses.last_kwargs.get("extra_body", {})


# ---------------------------------------------------------------------------
# I/O matrix: table chunks
# ---------------------------------------------------------------------------


def test_table_chunk_is_re_rendered_from_metadata() -> None:
    """A result chunk renders from ``columns``/``preview_rows``, not its Markdown."""
    client = FakeClient(
        [
            _item_done(
                _text_item(
                    FakeTextPart(
                        text="| unstable | model markdown |",
                        metadata={
                            "columns": [{"name": "region"}, {"name": "n"}],
                            "preview_rows": [["EMEA", "1200"]],
                        },
                    )
                )
            ),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert _texts(events) == ["| region | n |\n| --- | --- |\n| EMEA | 1200 |"]
    assert "unstable" not in (_response(events) or "")


def test_table_chunk_without_metadata_emits_the_raw_text() -> None:
    """No metadata → Genie's own text is all there is, so it passes through."""
    client = FakeClient([_item_done(_text_item(FakeTextPart(text="just prose"))), _completed()])
    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))
    assert _texts(events) == ["just prose"]


def test_fetch_failed_metadata_emits_the_raw_text() -> None:
    """``status: fetch_failed`` disqualifies the rows even when some are present.

    The stale preview below would otherwise render, so the raw text winning is
    the status check doing its job — not the empty-metadata fallback.
    """
    client = FakeClient(
        [
            _item_done(
                _text_item(
                    FakeTextPart(
                        text="Result unavailable.",
                        metadata={
                            "status": "fetch_failed",
                            "columns": ["stale"],
                            "preview_rows": [["do not render"]],
                        },
                    )
                )
            ),
            _completed(),
        ]
    )
    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))
    assert _texts(events) == ["Result unavailable."]


def test_metadata_without_rows_falls_back_to_the_raw_text() -> None:
    """Metadata that renders nothing must not blank out the chunk."""
    client = FakeClient(
        [
            _item_done(
                _text_item(
                    FakeTextPart(text="no rows", metadata={"columns": [{"name": "a"}]}),
                )
            ),
            _completed(),
        ]
    )
    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))
    assert _texts(events) == ["no rows"]


# ---------------------------------------------------------------------------
# Separator: report parts must open their own Markdown block
# ---------------------------------------------------------------------------


def test_table_part_after_prose_starts_a_fresh_markdown_block() -> None:
    """A table glued to the end of a sentence is not a table to any renderer."""
    client = FakeClient(
        [
            _item_done(
                _text_item(
                    FakeTextPart(text="Here are your top customers [1](u)."),
                    FakeTextPart(
                        text="| ignored |",
                        metadata={
                            "columns": ["customer"],
                            "preview_rows": [["Acme"]],
                        },
                    ),
                )
            ),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    chunks = _texts(events)
    assert chunks[1].startswith("\n\n| customer |")
    assert (
        _response(events)
        == "Here are your top customers [1](u).\n\n| customer |\n| --- |\n| Acme |"
    )


def test_part_already_ending_in_a_newline_gains_exactly_one() -> None:
    """Topping up to a blank line must not produce three consecutive newlines."""
    client = FakeClient(
        [
            _item_done(_text_item(FakeTextPart(text="first line\n"), FakeTextPart(text="second"))),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert _texts(events) == ["first line\n", "\nsecond"]
    assert _response(events) == "first line\n\nsecond"


def test_separator_spans_separate_message_items() -> None:
    """Parts split across two message items are separated just the same."""
    client = FakeClient(
        [
            _item_done(_text_item(FakeTextPart(text="prose"))),
            _item_done(_text_item(FakeTextPart(text="more"))),
            _completed(),
        ]
    )
    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))
    assert _texts(events) == ["prose", "\n\nmore"]


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        ("", ""),  # first chunk of the turn — nothing to separate from
        ("prose", "\n\n"),  # no trailing newline → open a blank line
        ("prose\n", "\n"),  # one already there → top up to two
        ("prose\n\n", ""),  # already a blank line → add nothing
        ("prose\n\n\n", ""),  # more than enough → never trim
    ],
)
def test_block_separator(previous: str, expected: str) -> None:
    assert _block_separator(previous) == expected


def test_turn_complete_equals_the_concatenated_text_chunks() -> None:
    """The load-bearing invariant: policy reads TurnComplete, the UI reads chunks."""
    client = FakeClient(
        [
            _item_done(_text_item(FakeTextPart(text="alpha"))),
            _item_done(
                _text_item(
                    FakeTextPart(text="beta\n"),
                    FakeTextPart(
                        text="| md |",
                        metadata={"columns": ["c"], "preview_rows": [["v"]]},
                    ),
                )
            ),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert _response(events) == "".join(_texts(events))


# ---------------------------------------------------------------------------
# I/O matrix: tool-call correlation
# ---------------------------------------------------------------------------


def test_call_and_output_share_one_non_empty_call_id() -> None:
    """The adapter pairs strictly by ``call_id``; an unpaired event is dropped."""
    client = FakeClient(
        [
            _item_done(
                FakeFunctionCallItem(call_id="call_abc", arguments='{"query": "SELECT 1"}')
            ),
            _item_done({"type": "function_call_output", "call_id": "call_abc", "output": "ok"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    request = next(e for e in events if isinstance(e, ToolCallRequest))
    complete = next(e for e in events if isinstance(e, ToolCallComplete))
    assert request.metadata["call_id"] == complete.metadata["call_id"] == "call_abc"
    assert request.name == complete.name == "execute_sql"
    assert request.args == {"query": "SELECT 1"}
    assert complete.result == "ok"


def test_function_call_output_arriving_as_a_raw_dict_still_correlates() -> None:
    """A raw-dict item correlates exactly like a parsed model does.

    Agent mode is Beta and may stream item variants the installed SDK does not
    model, which arrive unvalidated as plain dicts; key access must work exactly
    like attribute access or those SQL calls render as perpetual in-progress
    cards.
    """
    client = FakeClient(
        [
            _item_done(
                {"type": "function_call", "call_id": "c9", "name": "run_sql", "arguments": "{}"}
            ),
            _item_done({"type": "function_call_output", "call_id": "c9", "output": {"rows": 2}}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    request = next(e for e in events if isinstance(e, ToolCallRequest))
    complete = next(e for e in events if isinstance(e, ToolCallComplete))
    assert request.metadata["call_id"] == complete.metadata["call_id"] == "c9"
    assert request.name == complete.name == "run_sql"
    assert complete.result == {"rows": 2}


def test_id_less_call_and_output_are_paired_by_position() -> None:
    """Genie may omit ids; a synthesized one still has to be shared and non-empty."""
    client = FakeClient(
        [
            _item_done({"type": "function_call", "arguments": "{}"}),
            _item_done({"type": "function_call_output", "output": "ok"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    request = next(e for e in events if isinstance(e, ToolCallRequest))
    complete = next(e for e in events if isinstance(e, ToolCallComplete))
    assert request.metadata["call_id"]
    assert request.metadata["call_id"] == complete.metadata["call_id"]
    # No name on either item → the observed-SQL default.
    assert request.name == "execute_sql"


def test_mixed_explicit_and_id_less_outputs_each_complete_their_own_call() -> None:
    """An id-less output takes the oldest call still awaiting one, not the next by index.

    Genie answers the second call first here; positional pairing that counted
    every output would hand the id-less one to that same call twice and leave
    the first call hanging.
    """
    client = FakeClient(
        [
            _item_done({"type": "function_call", "arguments": "{}"}),  # id-less → synthesized
            _item_done({"type": "function_call", "call_id": "b", "arguments": "{}"}),
            _item_done({"type": "function_call_output", "call_id": "b", "output": "second"}),
            _item_done({"type": "function_call_output", "output": "first"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    completions = [e for e in events if isinstance(e, ToolCallComplete)]
    synthesized = requests[0].metadata["call_id"]
    assert synthesized and synthesized != "b"
    assert [e.metadata["call_id"] for e in completions] == ["b", synthesized]
    assert [e.result for e in completions] == ["second", "first"]


def test_output_naming_an_unknown_call_is_dropped() -> None:
    """An id we never issued must not produce a completion against another call."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="known")),
            _item_done({"type": "function_call_output", "call_id": "stranger", "output": "?"}),
            _item_done({"type": "function_call_output", "call_id": "known", "output": "ok"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    completions = [e for e in events if isinstance(e, ToolCallComplete)]
    assert [e.metadata["call_id"] for e in completions] == ["known"]
    assert completions[0].result == "ok"


def test_second_output_for_a_finished_call_is_dropped() -> None:
    """One call, one completion — a repeat would render a duplicate result card."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="c1")),
            _item_done({"type": "function_call_output", "call_id": "c1", "output": "first"}),
            _item_done({"type": "function_call_output", "call_id": "c1", "output": "again"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    completions = [e for e in events if isinstance(e, ToolCallComplete)]
    assert [e.result for e in completions] == ["first"]


def test_uncorrelatable_output_is_dropped_not_ghost_emitted() -> None:
    """An output with no id and no preceding call cannot pair, so it is dropped."""
    client = FakeClient(
        [
            _item_done({"type": "function_call_output", "output": "orphan"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TurnComplete"]


def test_malformed_call_arguments_are_preserved_not_assumed() -> None:
    """Genie documents ``arguments`` keys as unstable, so non-JSON is kept raw."""
    client = FakeClient(
        [_item_done(FakeFunctionCallItem(arguments="not json at all")), _completed()]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    request = next(e for e in events if isinstance(e, ToolCallRequest))
    assert request.args == {"raw": "not json at all"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),  # absent → nothing to show
        ("", {}),  # empty string → nothing to show
        ({"query": "SELECT 1"}, {"query": "SELECT 1"}),  # already a mapping
        ('{"query": "SELECT 1"}', {"query": "SELECT 1"}),  # JSON object
        ('["SELECT 1"]', {"raw": ["SELECT 1"]}),  # JSON, but not an object
        (["SELECT 1"], {"raw": ["SELECT 1"]}),  # already decoded, not an object
        (7, {"raw": 7}),  # a scalar is still worth showing
        (False, {"raw": False}),  # falsy but present
    ],
)
def test_parse_arguments_never_silently_drops_a_present_value(
    raw: object, expected: dict[str, Any]
) -> None:
    """Unstable ``arguments`` shapes are preserved under ``raw``, not discarded."""
    assert _parse_arguments(raw) == expected


def test_call_with_item_id_but_no_call_id_correlates_by_that_id() -> None:
    """The wire's ``id`` is the fallback correlation key when ``call_id`` is absent."""
    client = FakeClient(
        [
            _item_done({"type": "function_call", "id": "fc_9", "arguments": "{}"}),
            _item_done({"type": "function_call_output", "call_id": "fc_9", "output": "ok"}),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    request = next(e for e in events if isinstance(e, ToolCallRequest))
    completion = next(e for e in events if isinstance(e, ToolCallComplete))
    assert request.metadata["call_id"] == "fc_9"
    assert completion.metadata["call_id"] == "fc_9"
    assert completion.result == "ok"


def test_list_shaped_output_flattens_to_its_text() -> None:
    """A parts-list ``output`` renders as the query result, not a parts dump."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="c1")),
            _item_done(
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": [
                        {"type": "input_text", "text": "42 "},
                        {"type": "input_text", "text": "rows"},
                    ],
                }
            ),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    completion = next(e for e in events if isinstance(e, ToolCallComplete))
    assert completion.result == "42 rows"


def test_output_payload_keeps_text_less_values_verbatim() -> None:
    """A payload with no extractable text (or a raw dict) passes through untouched."""
    image_parts = [{"type": "input_image", "image_url": "u"}]
    assert _output_payload(image_parts) is image_parts
    raw = {"rows": 2}
    assert _output_payload(raw) is raw
    assert _output_payload("already text") == "already text"


# ---------------------------------------------------------------------------
# I/O matrix: failures and guard rails
# ---------------------------------------------------------------------------


def test_response_failed_emits_one_error_and_no_turn_complete() -> None:
    """A terminal failure names ``type``, ``code``, and ``message`` — and stops."""
    client = FakeClient(
        [
            _created("conv-1"),
            _failed(type="INVALID_STATE", code="RESOURCE_EXHAUSTED", message="warehouse is down"),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "type=INVALID_STATE" in events[0].message
    assert "code=RESOURCE_EXHAUSTED" in events[0].message
    assert "warehouse is down" in events[0].message


def test_error_event_is_terminal_like_a_failed_response() -> None:
    """A bare ``error`` event carries its fields at the top level, not under ``response``."""
    client = FakeClient(
        [
            _created("conv-1"),
            _error_event(code="INTERNAL_ERROR", message="genie exploded"),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["ExecutorError"]
    assert "code=INTERNAL_ERROR" in events[0].message
    assert "genie exploded" in events[0].message


def test_response_incomplete_errors_and_names_the_reason() -> None:
    """Genie stopping early is a failed turn, not a short answer."""
    client = FakeClient(
        [
            _item_done(_text_item(FakeTextPart(text="half an answer"))),
            _incomplete("max_output_tokens"),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TextChunk", "ExecutorError"]
    assert "max_output_tokens" in events[-1].message


def test_response_incomplete_without_details_still_errors() -> None:
    """No ``incomplete_details`` → still terminal, just without a named reason."""
    client = FakeClient([_created("conv-1"), _incomplete()])

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["ExecutorError"]
    assert "reason=" not in events[0].message


def test_stream_ending_without_a_terminal_event_is_not_a_completed_turn() -> None:
    """A dropped connection truncates the report; reporting success would hide that."""
    client = FakeClient([_created("conv-1"), _item_done(_text_item(FakeTextPart(text="half")))])

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TextChunk", "ExecutorError"]
    assert "ended before Genie completed the turn" in events[-1].message


def test_unresolved_sql_call_is_cancelled_when_the_turn_fails() -> None:
    """An unanswered call would otherwise render as a permanent in-progress card."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="call_x")),
            _failed(type="INTERNAL", code="X", message="boom"),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == [
        "ToolCallRequest",
        "ToolCallComplete",
        "ExecutorError",
    ]
    cancelled = events[1]
    assert cancelled.status is ToolCallStatus.CANCELLED
    assert cancelled.metadata["call_id"] == "call_x"
    assert cancelled.name == "execute_sql"


def test_resolved_sql_call_is_not_cancelled_when_the_stream_truncates() -> None:
    """Only calls still awaiting an output are cancelled — no duplicate cards."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="c1")),
            _item_done({"type": "function_call_output", "call_id": "c1", "output": "ok"}),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    completions = [e for e in events if isinstance(e, ToolCallComplete)]
    assert [e.status for e in completions] == [ToolCallStatus.SUCCESS]
    assert [type(e).__name__ for e in events][-1] == "ExecutorError"


def test_unresolved_sql_call_is_cancelled_when_the_turn_completes() -> None:
    """Success must not strand a card either: an unanswered call is cancelled
    before ``TurnComplete``, or its SQL card would spin forever on a turn that
    looks perfectly healthy."""
    client = FakeClient(
        [
            _item_done(FakeFunctionCallItem(call_id="call_x")),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == [
        "ToolCallRequest",
        "ToolCallComplete",
        "TurnComplete",
    ]
    cancelled = events[1]
    assert cancelled.status is ToolCallStatus.CANCELLED
    assert cancelled.metadata["call_id"] == "call_x"


def test_feature_disabled_404_points_at_the_beta_preview() -> None:
    """Agent mode is Beta; a 404 FEATURE_DISABLED is a workspace toggle, not a bug."""
    client = FakeClient(error=_Status4xx(404, "FEATURE_DISABLED: genie agents"))

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "Beta" in events[0].message
    assert "preview" in events[0].message


def test_plain_404_does_not_claim_the_preview_is_disabled() -> None:
    """A 404 without FEATURE_DISABLED is a bad space id, so keep the generic text."""
    client = FakeClient(error=_Status4xx(404, "space not found"))
    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))
    assert "Beta" not in events[0].message
    assert "space not found" in events[0].message


def test_conflict_409_names_the_in_progress_conversation() -> None:
    """409 means the previous turn is still generating for this conversation."""
    client = FakeClient([_created("conv-77"), _completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=client)
    _events(executor, [_user("first")])

    client.responses.error = _Status4xx(409, "RESOURCE_CONFLICT")
    events = _events(executor, [_user("second")])

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "conv-77" in events[0].message
    assert "in progress" in events[0].message


def test_stream_error_mid_turn_becomes_one_executor_error() -> None:
    """A transport failure while reading the stream ends the turn cleanly."""

    def _explode() -> Any:
        yield _item_done(_text_item(FakeTextPart(text="partial")))
        raise RuntimeError("connection reset")

    client = FakeClient(_explode())

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TextChunk", "ExecutorError"]
    assert "connection reset" in events[-1].message


def test_missing_space_id_errors_without_any_http_call() -> None:
    """No space id → an actionable ExecutorError, and the endpoint is never hit."""
    client = FakeClient([_completed()])

    events = _events(DatabricksGenieExecutor(space_id=None, client=client))

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "executor.model" in events[0].message
    assert client.responses.calls == []


def test_blank_user_text_errors_without_any_http_call() -> None:
    """A whitespace-only prompt is not worth a request."""
    client = FakeClient([_completed()])

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client), [_user("   ")])

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no user message" in events[0].message
    assert client.responses.calls == []


def test_history_without_a_user_message_errors_without_any_http_call() -> None:
    """An assistant-only history hits the same guard."""
    client = FakeClient([_completed()])

    events = _events(
        DatabricksGenieExecutor(space_id="sp", client=client),
        [{"role": "assistant", "content": "hello"}],
    )

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert client.responses.calls == []


def test_empty_report_completes_with_an_empty_response() -> None:
    """A stream with no text yields TurnComplete("") and no empty TextChunk."""
    client = FakeClient([_created("conv-1"), _completed()])

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TurnComplete"]
    assert events[0].response == ""


def test_unknown_event_types_are_ignored() -> None:
    """Genie's Beta stream may carry events we do not map; they must not break it."""
    client = FakeClient(
        [
            FakeEvent("response.in_progress"),
            FakeEvent("response.output_item.added", item=FakeFunctionCallItem()),
            _item_done(_text_item(FakeTextPart(text="done"))),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert [type(e).__name__ for e in events] == ["TextChunk", "TurnComplete"]


def test_unknown_item_types_are_dropped_without_breaking_the_turn() -> None:
    """An unmapped output item — a viz item under ``enable_viz`` is today's
    occupant of this branch — is skipped; the rest of the turn stays intact."""
    client = FakeClient(
        [
            _item_done({"type": "visualization", "spec": {"chart": "bar"}}),
            _item_done(_text_item(FakeTextPart(text="report"))),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client, enable_viz=True))

    assert [type(e).__name__ for e in events] == ["TextChunk", "TurnComplete"]
    assert _response(events) == "report"


def test_non_output_text_parts_are_skipped() -> None:
    """A message part that is not ``output_text`` (e.g. a refusal) is not report text."""
    client = FakeClient(
        [
            _item_done(
                _text_item(
                    FakeTextPart(text="shown"),
                    FakeTextPart(text="hidden", type="refusal"),
                )
            ),
            _completed(),
        ]
    )

    events = _events(DatabricksGenieExecutor(space_id="sp", client=client))

    assert _texts(events) == ["shown"]


# ---------------------------------------------------------------------------
# Lazy client construction
# ---------------------------------------------------------------------------


class _FakeAuth(httpx.Auth):
    """Stand-in for the refreshing Databricks bearer auth."""

    token = "dapi-secret-token"

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


@pytest.fixture
def _stub_databricks_auth(monkeypatch: pytest.MonkeyPatch) -> _FakeAuth:
    """Resolve Databricks credentials to a fake auth + host, with no I/O."""
    auth = _FakeAuth()
    monkeypatch.setattr(
        databricks_executor,
        "_resolve_databricks_auth",
        lambda profile=None, **_: (auth, "https://ws.example.com/"),
    )
    return auth


def test_lazy_client_is_built_once_and_reused(_stub_databricks_auth: _FakeAuth) -> None:
    """The client is constructed on first use and shared by every later turn."""
    executor = DatabricksGenieExecutor(space_id="space-1", profile="dev")

    client = executor._ensure_client("space-1")
    try:
        assert executor._ensure_client("space-1") is client
        assert str(client.base_url).rstrip("/") == (
            "https://ws.example.com/api/2.0/genie/agents/space-1"
        )
    finally:
        client.close()


def test_lazy_client_never_snapshots_a_token(_stub_databricks_auth: _FakeAuth) -> None:
    """Auth rides the per-request httpx hook so OAuth refresh keeps working."""
    executor = DatabricksGenieExecutor(space_id="space-1", profile="dev")

    client = executor._ensure_client("space-1")
    try:
        assert client.api_key == _OPENAI_KEY_PLACEHOLDER
        assert _FakeAuth.token not in str(client.api_key)
        # ``_client`` is the OpenAI SDK's httpx client; the auth hook living
        # there is what makes the token per-request rather than a snapshot.
        assert client._client.auth is _stub_databricks_auth
    finally:
        client.close()


def test_lazy_client_applies_the_stream_idle_timeout_and_disables_retries(
    _stub_databricks_auth: _FakeAuth,
) -> None:
    """Genie's 409 is in the SDK's default retry set but is never retryable."""
    executor = DatabricksGenieExecutor(space_id="space-1", timeout_seconds=900.0)

    client = executor._ensure_client("space-1")
    try:
        assert client.timeout == 900.0
        assert client.max_retries == 0
    finally:
        client.close()


def test_close_closes_an_owned_client_and_is_safe_twice(_stub_databricks_auth: _FakeAuth) -> None:
    """Harness shutdown must release the connection pool this executor opened."""
    executor = DatabricksGenieExecutor(space_id="space-1")
    http_client = executor._ensure_client("space-1")._client  # type: ignore[attr-defined]

    _run(executor.close())

    assert http_client.is_closed
    assert executor._client is None
    _run(executor.close())  # second call must not raise


def test_close_leaves_an_injected_client_alone() -> None:
    """A client we did not build belongs to its caller, so closing must not touch it."""
    injected = FakeClient([_completed()])
    executor = DatabricksGenieExecutor(space_id="sp", client=injected)

    _run(executor.close())

    assert executor._client is injected


def test_close_without_ever_building_a_client_is_a_no_op() -> None:
    """A turn-less executor still gets closed at shutdown."""
    _run(DatabricksGenieExecutor(space_id="sp").close())


def test_failed_client_construction_does_not_orphan_the_http_client(
    _stub_databricks_auth: _FakeAuth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing else holds the pool if the OpenAI constructor rejects our arguments."""
    built: list[httpx.Client] = []
    real_client = httpx.Client

    def _record(**kwargs: Any) -> httpx.Client:
        client = real_client(**kwargs)
        built.append(client)
        return client

    def _reject(**_: Any) -> Any:
        raise TypeError("unexpected keyword argument")

    monkeypatch.setattr(httpx, "Client", _record)
    monkeypatch.setattr("openai.OpenAI", _reject)

    executor = DatabricksGenieExecutor(space_id="sp")
    with pytest.raises(TypeError):
        executor._ensure_client("sp")

    assert [c.is_closed for c in built] == [True]
    assert executor._http_client is None


def test_client_construction_runs_off_the_event_loop() -> None:
    """Credential resolution blocks (file I/O + a token mint), so it needs a thread."""
    built_on: list[int] = []
    executor = DatabricksGenieExecutor(space_id="sp")

    def _record(agent_id: str) -> object:
        built_on.append(threading.get_ident())
        return FakeClient([_completed()])

    executor._ensure_client = _record  # type: ignore[method-assign]

    async def _collect() -> int:
        async for _ in executor.run_turn([_user("q")], [], "SYS"):
            pass
        return threading.get_ident()

    loop_thread = _run(_collect())

    assert built_on and built_on[0] != loop_thread


def test_missing_databricks_sdk_names_the_package_and_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the SDK there is no way to authenticate; say what to install."""
    real_import = __import__

    def _no_databricks_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("databricks.sdk"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "databricks.sdk.config", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_databricks_sdk)

    events = _events(DatabricksGenieExecutor(space_id="sp"))

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "databricks-sdk" in events[0].message
    assert "omnigent[databricks]" in events[0].message
    # The setup message is self-describing and must NOT be buried behind the
    # generic "databricks-genie request failed:" prefix.
    assert events[0].message.startswith("the databricks-sdk package is required")


def test_concurrent_first_turns_build_exactly_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers racing the lazy build share one client; no pool leaks.

    A cancelled first turn can overlap the next turn's ``to_thread`` worker, so
    the check-then-build must be atomic — the loser of an unguarded race would
    leak its httpx connection pool for the process lifetime.
    """
    auth = _FakeAuth()

    def _slow_resolve(profile: Any = None, **_: Any) -> tuple[Any, str]:
        time.sleep(0.2)
        return auth, "https://ws.example.com/"

    monkeypatch.setattr(databricks_executor, "_resolve_databricks_auth", _slow_resolve)
    built: list[httpx.Client] = []

    class _CountingClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self)

    monkeypatch.setattr(httpx, "Client", _CountingClient)
    executor = DatabricksGenieExecutor(space_id="sp")

    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(executor._ensure_client("sp")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert len(built) == 1
        assert len(results) == 2
        assert results[0] is results[1]
    finally:
        _run(executor.close())
    assert all(client.is_closed for client in built)


# ---------------------------------------------------------------------------
# Interrupt: closing the live stream unblocks a cancelled turn
# ---------------------------------------------------------------------------


class _BlockingClosableStream:
    """Iterator that replays a prefix, then blocks until :meth:`close`."""

    def __init__(self, prefix: list[Any]) -> None:
        self._items = iter(prefix)
        self._unblock = threading.Event()
        self.closed = False

    def __iter__(self) -> _BlockingClosableStream:
        return self

    def __next__(self) -> Any:
        for item in self._items:
            return item
        self._unblock.wait(timeout=5.0)
        raise RuntimeError("stream closed" if self.closed else "stream never closed")

    def close(self) -> None:
        self.closed = True
        self._unblock.set()


def test_interrupt_session_with_no_active_stream_returns_false() -> None:
    """Nothing in flight → nothing to interrupt."""
    executor = DatabricksGenieExecutor(space_id="sp", client=FakeClient([_completed()]))
    assert _run(executor.interrupt_session("k")) is False


def test_interrupt_session_mid_stream_closes_the_stream_and_unblocks_the_turn() -> None:
    """Closing the stream ends a turn parked mid-query with cancellations + one error."""
    stream = _BlockingClosableStream(
        [_created("conv-1"), _item_done(FakeFunctionCallItem(call_id="c1"))]
    )
    executor = DatabricksGenieExecutor(space_id="sp", client=FakeClient(stream))

    async def _scenario() -> list[Any]:
        events: list[Any] = []

        async def _consume() -> None:
            async for event in executor.run_turn([_user("q")], [], "SYS"):
                events.append(event)

        task = asyncio.ensure_future(_consume())
        while not events:  # wait until the SQL call surfaced, then interrupt
            await asyncio.sleep(0.01)
        assert await executor.interrupt_session("k") is True
        await asyncio.wait_for(task, timeout=5.0)
        return events

    events = _run(_scenario())

    assert stream.closed
    assert [type(e).__name__ for e in events] == [
        "ToolCallRequest",
        "ToolCallComplete",
        "ExecutorError",
    ]
    assert events[1].status is ToolCallStatus.CANCELLED
    assert events[1].metadata["call_id"] == "c1"


def test_interrupt_session_twice_is_safe_and_clears_after_the_turn() -> None:
    """Double interrupt is idempotent; a finished turn leaves nothing to close."""
    stream = _BlockingClosableStream([_created("conv-1")])
    executor = DatabricksGenieExecutor(space_id="sp", client=FakeClient(stream))

    async def _scenario() -> None:
        events: list[Any] = []

        async def _consume() -> None:
            async for event in executor.run_turn([_user("q")], [], "SYS"):
                events.append(event)

        task = asyncio.ensure_future(_consume())
        while executor._active_stream is None:
            await asyncio.sleep(0.01)
        assert await executor.interrupt_session("k") is True
        assert await executor.interrupt_session("k") is True
        await asyncio.wait_for(task, timeout=5.0)
        assert await executor.interrupt_session("k") is False

    _run(_scenario())


def test_interrupt_session_tolerates_a_stream_without_close() -> None:
    """A close-less iterator (the test fakes' shape) must not make interrupt raise."""
    executor = DatabricksGenieExecutor(space_id="sp", client=FakeClient([]))
    executor._active_stream = iter([])
    assert _run(executor.interrupt_session("k")) is True


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


def test_capability_flags() -> None:
    """Streaming is real; Genie runs its own SQL so Session must not re-dispatch."""
    executor = DatabricksGenieExecutor(space_id="sp")
    assert executor.supports_streaming() is True
    assert executor.supports_tool_calling() is False
    assert executor.handles_tools_internally() is True


# ---------------------------------------------------------------------------
# Pure helpers: message extraction
# ---------------------------------------------------------------------------


def test_extract_text_edge_cases() -> None:
    """``_extract_text`` covers absent, string, multimodal, and non-text content."""
    assert _extract_text({"role": "user"}) == ""  # no content key → None
    assert _extract_text({"content": None}) == ""
    assert _extract_text({"content": "plain"}) == "plain"
    assert _extract_text({"content": [{"type": "text", "text": "a"}, {"no": "text"}]}) == "a"
    # A non-dict element in the parts list is skipped, not coerced.
    assert _extract_text({"content": ["skip-me", {"type": "text", "text": "a"}]}) == "a"
    # Non-str / non-list content falls back to str().
    assert _extract_text({"content": 123}) == "123"


def test_latest_user_text_no_user_message_returns_empty() -> None:
    """With no user-role message, the latest-user lookup yields ``""``."""
    assert _latest_user_text([{"role": "assistant", "content": "hi"}]) == ""
    assert _latest_user_text([]) == ""


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"content": "Plan it."}, "Plan it."),  # text directly on ``content``
        ({"summary": "Summarized."}, "Summarized."),  # …or on ``summary``
        ({"content": [{"text": "a"}, {"text": "b"}]}, "a\nb"),  # list of parts
        ({"content": ["a", "b"]}, "a\nb"),  # list of bare strings
        ({"content": "", "summary": "fallback"}, "fallback"),  # empty → next field
        ({"content": "  ", "summary": "fallback"}, "fallback"),  # whitespace-only too
        ({"content": ["  "], "summary": "fallback"}, "fallback"),  # …and as parts
        ({"content": [], "text": "last resort"}, "last resort"),  # neither field → ``text``
        ({}, ""),  # nothing to show
    ],
)
def test_reasoning_text_reads_either_shape(item: dict[str, Any], expected: str) -> None:
    """Genie may carry planning as a string or a list of parts; both must surface."""
    assert _reasoning_text(item) == expected


def test_latest_user_text_takes_the_last_user_turn_and_joins_parts() -> None:
    """The newest user message wins, and list-of-parts content is joined."""
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image"}]},
    ]
    assert _latest_user_text(messages) == "hello"


# ---------------------------------------------------------------------------
# Pure helpers: table formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),  # None → empty cell
        ("plain", "plain"),  # passthrough
        ("a\nb\tc  d", "a b c d"),  # newline/tab/double-space collapse to single spaces
        ("  trim  ", "trim"),  # leading/trailing whitespace stripped
        ("a|b", r"a\|b"),  # pipe escaped so it doesn't open a column
        # A pre-escaped pipe would otherwise become ``\\|`` — an escaped
        # backslash before a LIVE delimiter — so backslashes escape first.
        ("a\\|b", "a\\\\\\|b"),
        ("C:\\*.txt", "C:\\\\*.txt"),  # a lone backslash is doubled, not swallowed
        (1200, "1200"),  # non-str coerced via str()
    ],
)
def test_md_cell(value: object, expected: str) -> None:
    assert _md_cell(value) == expected


def test_render_table_without_rows_returns_empty() -> None:
    assert _render_table({"columns": ["a"], "preview_rows": []}) == ""
    assert _render_table({"columns": ["a"]}) == ""
    assert _render_table(None) == ""


def test_render_table_emits_valid_gfm() -> None:
    """Header row, delimiter row, then one line per preview row."""
    metadata = {
        "columns": [{"name": "region"}, {"name": "n"}],
        "preview_rows": [["EMEA", "1200"]],
    }
    assert _render_table(metadata) == "| region | n |\n| --- | --- |\n| EMEA | 1200 |"


def test_render_table_accepts_plain_string_columns() -> None:
    """Genie may name columns as bare strings rather than objects."""
    metadata = {"columns": ["a", "b"], "preview_rows": [["1", "2"]]}
    assert _render_table(metadata) == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_render_table_accepts_keyed_rows() -> None:
    """Rows keyed by column name are ordered by the header, not by dict order."""
    metadata = {
        "columns": ["first", "second"],
        "preview_rows": [{"second": "B", "first": "A"}],
    }
    assert _render_table(metadata) == "| first | second |\n| --- | --- |\n| A | B |"


def test_render_table_without_columns_synthesizes_headers() -> None:
    """With no column metadata, generic ``col1, col2`` keeps the table valid GFM."""
    metadata = {"preview_rows": [["a", "b"]]}
    assert _render_table(metadata) == "| col1 | col2 |\n| --- | --- |\n| a | b |"


def test_render_table_takes_headers_from_keyed_rows_when_columns_are_absent() -> None:
    """Keyed rows know their own labels; without them the row renders as blanks."""
    metadata = {"preview_rows": [{"region": "EMEA", "n": "3"}]}
    assert _render_table(metadata) == "| region | n |\n| --- | --- |\n| EMEA | 3 |"


def test_render_table_pads_ragged_rows() -> None:
    """A row shorter than the column count is padded with empty cells."""
    metadata = {"columns": ["a", "b"], "preview_rows": [["x"]]}
    assert _render_table(metadata) == "| a | b |\n| --- | --- |\n| x |  |"


def test_render_table_sanitizes_multiline_and_piped_cells() -> None:
    """A multi-line / pipe-bearing cell is collapsed so the table holds together."""
    metadata = {"columns": ["review"], "preview_rows": [["line one\nline two | tail"]]}
    assert _render_table(metadata) == "| review |\n| --- |\n| line one line two \\| tail |"


def test_render_table_truncates_beyond_the_row_cap() -> None:
    """Genie previews upstream; this is our own bound on what we echo back."""
    rows = [[str(i)] for i in range(_MAX_RESULT_ROWS + 5)]
    rendered = _render_table({"columns": ["i"], "preview_rows": rows})

    assert "… (5 more rows)" in rendered
    assert f"| {_MAX_RESULT_ROWS - 1} |" in rendered
    assert f"| {_MAX_RESULT_ROWS} |" not in rendered


def test_render_table_single_omitted_row_is_singular() -> None:
    rows = [[str(i)] for i in range(_MAX_RESULT_ROWS + 1)]
    assert "… (1 more row)" in _render_table({"columns": ["i"], "preview_rows": rows})


def test_render_table_reports_rows_genie_previewed_away_upstream() -> None:
    """``preview_rows`` is itself a preview: ``total_row_count`` counts the full
    result, so a 2-row preview of a 12,000-row result must say so."""
    metadata = {
        "columns": ["customer"],
        "preview_rows": [["Acme"], ["Globex"]],
        "total_row_count": 12000,
    }
    assert "… (11998 more rows)" in _render_table(metadata)


def test_render_table_with_total_matching_the_preview_adds_no_footer() -> None:
    """A complete preview (the common case on the wire) stays footer-free."""
    metadata = {
        "columns": ["customer"],
        "preview_rows": [["Acme"], ["Globex"]],
        "total_row_count": 2,
    }
    assert "more row" not in _render_table(metadata)


def test_render_table_combines_upstream_total_with_the_local_cap() -> None:
    """Local capping and upstream truncation fold into one honest footer."""
    rows = [[str(i)] for i in range(_MAX_RESULT_ROWS + 5)]
    rendered = _render_table({"columns": ["i"], "preview_rows": rows, "total_row_count": 100})
    assert f"… ({100 - _MAX_RESULT_ROWS} more rows)" in rendered


def test_render_table_ignores_a_malformed_total_row_count() -> None:
    """A non-int total (Beta wire drift) falls back to counting preview rows."""
    metadata = {
        "columns": ["c"],
        "preview_rows": [["v"]],
        "total_row_count": "not-a-number",
    }
    assert "more row" not in _render_table(metadata)
