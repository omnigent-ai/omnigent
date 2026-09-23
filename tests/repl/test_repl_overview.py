"""Unit tests for the Ctrl+O overview builders in ``omnigent.repl._repl``.

Covers the sidebar target discovery, the ``[N] type=...`` event renderers, and
the overview assembly's error/empty states. The client and session are small
in-memory fakes; no server is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from omnigent_client import StaleCursorError
from omnigent_ui_sdk import OverlayTarget
from rich.console import Console, RenderableType

from omnigent.repl import _repl
from omnigent.repl._repl import (
    _build_debug_overview,
    _collect_overview_targets,
    _list_all_conversation_items,
    _render_overview_event,
    _render_overview_message_event,
)


class _Fmt:
    muted = "dim"
    accent = "cyan"
    error = "red"


def _text(lines: list[RenderableType] | RenderableType) -> str:
    renderables = lines if isinstance(lines, list) else [lines]
    console = Console(width=1000, no_color=True, file=None)
    with console.capture() as cap:
        for renderable in renderables:
            console.print(renderable)
    return cap.get()


def _handle(conv_id: str, *, agent: str = "worker", title: str = "task") -> str:
    return json.dumps(
        {"kind": "sub_agent", "conversation_id": conv_id, "agent": agent, "title": title}
    )


def _output_item(output: str, call_id: str = "c1") -> dict[str, object]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


class _Sessions:
    def __init__(
        self,
        *,
        pages: list[object] | None = None,
        labels: dict[str, str] | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.pages = list(pages or [])
        self.labels = labels or {}
        self.get_error = get_error
        self.list_calls: list[str | None] = []

    async def list_items(
        self, session_id: str, *, limit: int, after: str | None, order: str
    ) -> list[dict[str, object]]:
        self.list_calls.append(after)
        page = self.pages.pop(0) if self.pages else []
        if isinstance(page, Exception):
            raise page
        return page  # type: ignore[return-value]

    async def get(self, session_id: str) -> SimpleNamespace:
        if self.get_error is not None:
            raise self.get_error
        return SimpleNamespace(labels=self.labels)


class _Responses:
    def __init__(self, conversation_id: str | None = None, error: Exception | None = None) -> None:
        self.conversation_id = conversation_id
        self.error = error
        self.requested: list[str] = []

    async def get(self, response_id: str) -> SimpleNamespace:
        self.requested.append(response_id)
        if self.error is not None:
            raise self.error
        conversation = (
            SimpleNamespace(id=self.conversation_id) if self.conversation_id is not None else None
        )
        return SimpleNamespace(conversation=conversation)


def _client(
    sessions: _Sessions | None = None, responses: _Responses | None = None
) -> SimpleNamespace:
    return SimpleNamespace(sessions=sessions or _Sessions(), responses=responses or _Responses())


def _session(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {"current_response_id": None, "model": "m-1"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ── message / event renderers ────────────────────────────────


def test_message_event_labels_user_and_flattens_text_parts() -> None:
    item = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "first"},
            {"type": "input_image", "text": "ignored"},
            {"type": "input_text", "text": None},
            {"type": "input_text", "text": "second"},
            "not-a-dict",
        ],
    }

    text = _text(_render_overview_message_event(3, item, _Fmt()))

    assert "[3] type=user_message" in text
    assert "first second" in text
    assert "ignored" not in text


def test_message_event_shows_assistant_model_and_defaults_unknown_roles() -> None:
    assistant = _text(
        _render_overview_message_event(
            1, {"role": "assistant", "model": "gpt-x", "content": []}, _Fmt()
        )
    )
    no_role = _text(_render_overview_message_event(2, {"content": "not-a-list"}, _Fmt()))

    assert "type=assistant_message model=gpt-x" in assistant
    assert "type=assistant_message" in no_role
    assert "model=" not in no_role


def test_message_event_truncates_long_text() -> None:
    long_text = "x" * 500

    text = _text(
        _render_overview_message_event(
            1, {"role": "user", "content": [{"type": "input_text", "text": long_text}]}, _Fmt()
        )
    )

    assert "x" * 400 + "…" in text
    assert "x" * 401 not in text


def test_event_function_call_shows_name_and_arguments() -> None:
    item = {"type": "function_call", "name": "Bash", "arguments": '{"cmd": "ls"}'}

    text = _text(_render_overview_event(2, item, {}, _Fmt()))

    assert "[2] type=tool_call_request name=Bash" in text
    assert 'args: {"cmd": "ls"}' in text


def test_event_function_call_flags_missing_name_and_omits_empty_args() -> None:
    lines = _render_overview_event(1, {"type": "function_call", "arguments": ""}, {}, _Fmt())

    assert len(lines) == 1
    assert "name=(missing name)" in _text(lines)


def test_event_function_call_output_correlates_name_status_and_output() -> None:
    item = {
        "type": "function_call_output",
        "call_id": "c1",
        "status": "completed",
        "output": "line one\nline two",
    }

    text = _text(_render_overview_event(4, item, {"c1": "Bash"}, _Fmt()))

    assert "[4] type=tool_call_complete name=Bash" in text
    assert "status: completed" in text
    assert "line one" in text and "line two" in text


@pytest.mark.parametrize(
    ("item", "expected_name"),
    [
        ({"type": "function_call_output", "call_id": "c9"}, "(unknown)"),
        ({"type": "function_call_output"}, "(missing call_id)"),
    ],
)
def test_event_function_call_output_flags_uncorrelated_items(
    item: dict[str, object], expected_name: str
) -> None:
    text = _text(_render_overview_event(1, item, {"c1": "Bash"}, _Fmt()))

    assert f"name={expected_name}" in text


def test_event_function_call_output_truncates_long_output() -> None:
    item = {"type": "function_call_output", "call_id": "c1", "output": "y" * 450}

    text = _text(_render_overview_event(1, item, {"c1": "Bash"}, _Fmt()))

    assert "y" * 400 + "…" in text
    assert "y" * 401 not in text


def test_event_reasoning_prefers_summary_then_content_and_skips_blank_lines() -> None:
    summary = _text(
        _render_overview_event(
            1, {"type": "reasoning", "summary": "thinking\n\n  \nmore"}, {}, _Fmt()
        )
    )
    content = _text(_render_overview_event(1, {"type": "reasoning", "content": "raw"}, {}, _Fmt()))
    bare = _render_overview_event(1, {"type": "reasoning"}, {}, _Fmt())

    assert "type=reasoning" in summary and "thinking" in summary and "more" in summary
    assert "raw" in content
    assert len(bare) == 1


@pytest.mark.parametrize(
    ("item", "label"),
    [
        ({"type": "web_search_call"}, "web_search_call"),
        ({"type": ""}, "(unknown)"),
        ({"type": 7}, "(unknown)"),
        ({}, "(unknown)"),
    ],
)
def test_event_unknown_types_are_surfaced(item: dict[str, object], label: str) -> None:
    text = _text(_render_overview_event(5, item, {}, _Fmt()))

    assert f"[5] type={label}" in text


# ── item pagination ──────────────────────────────────────────


async def test_list_all_items_pages_until_short_page() -> None:
    first = [{"id": f"i{n}"} for n in range(100)]
    sessions = _Sessions(pages=[first, [{"id": "tail"}]])

    items = await _list_all_conversation_items(_client(sessions), "conv_a")  # type: ignore[arg-type]

    assert len(items) == 101
    assert sessions.list_calls == [None, "i99"]


async def test_list_all_items_restarts_from_first_page_on_stale_cursor() -> None:
    first = [{"id": f"i{n}"} for n in range(100)]
    sessions = _Sessions(
        pages=[first, StaleCursorError("gone", code="stale_cursor"), [{"id": "z"}]]
    )

    items = await _list_all_conversation_items(_client(sessions), "conv_a")  # type: ignore[arg-type]

    # The partial prefix from before the stale cursor is discarded.
    assert items == [{"id": "z"}]
    assert sessions.list_calls == [None, "i99", None]


async def test_list_all_items_gives_up_after_repeated_stale_cursors() -> None:
    stale = StaleCursorError("gone", code="stale_cursor")
    sessions = _Sessions(pages=[stale] * 10)

    items = await _list_all_conversation_items(_client(sessions), "conv_a")  # type: ignore[arg-type]

    assert items == []
    assert len(sessions.list_calls) == _repl._LIST_ITEMS_MAX_RESTARTS + 1


async def test_list_all_items_keeps_prefix_on_other_errors_and_missing_ids() -> None:
    first = [{"id": f"i{n}"} for n in range(100)]
    failing = _Sessions(pages=[first, RuntimeError("boom")])
    no_ids = _Sessions(pages=[[{"type": "message"}] * 100])

    assert len(await _list_all_conversation_items(_client(failing), "c")) == 100  # type: ignore[arg-type]
    assert len(await _list_all_conversation_items(_client(no_ids), "c")) == 100  # type: ignore[arg-type]
    assert no_ids.list_calls == [None]


# ── sidebar targets ──────────────────────────────────────────


async def test_targets_default_to_main_without_a_conversation() -> None:
    targets = await _collect_overview_targets(_client(), _session())  # type: ignore[arg-type]

    assert targets == [OverlayTarget(key="main", label="main", icon="🤖")]


async def test_targets_resolve_conversation_through_response_id() -> None:
    responses = _Responses(conversation_id="conv_root")
    client = _client(_Sessions(pages=[[]]), responses)

    targets = await _collect_overview_targets(client, _session(current_response_id="resp_1"))  # type: ignore[arg-type]

    assert responses.requested == ["resp_1"]
    assert targets == [OverlayTarget(key="conv_root", label="main", icon="🤖")]


@pytest.mark.parametrize(
    "responses",
    [_Responses(error=RuntimeError("down")), _Responses(conversation_id=None)],
)
async def test_targets_fall_back_to_main_when_response_lookup_fails(
    responses: _Responses,
) -> None:
    targets = await _collect_overview_targets(
        _client(responses=responses),  # type: ignore[arg-type]
        _session(current_response_id="resp_1"),
    )

    assert [t.key for t in targets] == ["main"]


async def test_targets_list_sub_agents_and_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _output_item(_handle("conv_a", agent="coder", title="fix")),
        # Continuation of the same child, non-string output, malformed handle,
        # and unrelated output are all skipped.
        _output_item(_handle("conv_a", agent="coder", title="fix")),
        {"type": "function_call_output", "output": {"not": "a string"}},
        {"type": "message"},
        _output_item(json.dumps({"kind": "sub_agent", "conversation_id": "conv_bad"})),
        _output_item(json.dumps({"kind": "sub_agent", "agent": "a", "title": "t"})),
        _output_item("plain text"),
        _output_item(_handle("conv_b", agent="tester", title="verify")),
    ]
    seen_seeds: list[dict[str, list[dict[str, object]]] | None] = []
    terminal = SimpleNamespace(name="bash", session="s1", conv_id="conv_root", socket="/tmp/s")

    async def fake_items(client: object, conv_id: str) -> list[dict[str, object]]:
        return items

    async def fake_terminals(
        client: object, conv_ids: list[str], *, seed_items: object = None
    ) -> list[SimpleNamespace]:
        seen_seeds.append((conv_ids, seed_items))  # type: ignore[arg-type]
        return [terminal]

    monkeypatch.setattr(_repl, "_list_all_conversation_items", fake_items)
    monkeypatch.setattr(_repl, "_collect_terminals_for_conversations", fake_terminals)
    monkeypatch.setattr(_repl, "_terminal_target_key", lambda info: f"terminal::{info.name}")

    targets = await _collect_overview_targets(_client(), _session(session_id="conv_root"))  # type: ignore[arg-type]

    assert [(t.key, t.label, t.icon) for t in targets] == [
        ("conv_root", "main", "🤖"),
        ("conv_a", "coder:fix", "👾"),
        ("conv_b", "tester:verify", "👾"),
        ("terminal::bash", "bash:s1", "💻"),
    ]
    # Terminals are gathered across the root and every sub-agent, reusing the
    # already-fetched root items.
    assert seen_seeds == [(["conv_root", "conv_a", "conv_b"], {"conv_root": items})]


async def test_targets_survive_item_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_items(client: object, conv_id: str) -> list[dict[str, object]]:
        raise RuntimeError("down")

    monkeypatch.setattr(_repl, "_list_all_conversation_items", failing_items)

    targets = await _collect_overview_targets(_client(), _session(session_id="conv_root"))  # type: ignore[arg-type]

    assert [(t.key, t.label) for t in targets] == [("conv_root", "main")]


# ── overview assembly ────────────────────────────────────────


async def _overview(
    target: OverlayTarget | None,
    client: SimpleNamespace,
    session: SimpleNamespace,
    **paths: object,
) -> str:
    result = await _build_debug_overview(
        target,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        agent_name="coder",
        fmt=_Fmt(),  # type: ignore[arg-type]
        **paths,  # type: ignore[arg-type]
    )
    return _text(result)


async def test_overview_for_fresh_repl_explains_state() -> None:
    text = await _overview(None, _client(), _session())

    assert "Session: main" in text
    assert "(no conversation yet)" in text
    assert "Agent: coder" in text and "Model: m-1" in text and "Response: (none yet)" in text
    assert "No conversation yet. Send a message to start one." in text


async def test_overview_lists_configured_log_paths(tmp_path) -> None:
    paths = {
        "server_log_path": tmp_path / "server.log",
        "runner_log_path": tmp_path / "runner.log",
        "event_log_path": tmp_path / "events.jsonl",
        "cli_log_path": tmp_path / "cli.log",
    }

    text = await _overview(None, _client(), _session(), **paths)

    for label, path in (
        ("Server log", paths["server_log_path"]),
        ("Runner log", paths["runner_log_path"]),
        ("Event log", paths["event_log_path"]),
        ("CLI log", paths["cli_log_path"]),
    ):
        assert f"{label}: {path}" in text.replace("\n", "")


async def test_overview_reports_response_lookup_failure() -> None:
    client = _client(responses=_Responses(error=RuntimeError("no such response")))

    text = await _overview(None, client, _session(current_response_id="resp_1"))

    assert "Response: resp_1" in text
    assert "Failed to resolve conversation: RuntimeError: no such response" in text


async def test_overview_resolves_conversation_from_response_and_renders_events() -> None:
    sessions = _Sessions(
        labels={"b": "2", "a": "1"},
        pages=[
            [
                {"type": "function_call", "call_id": "c1", "name": "Bash"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ]
        ],
    )
    client = _client(sessions, _Responses(conversation_id="conv_root"))

    text = await _overview(None, client, _session(current_response_id="resp_1"))

    assert "Session ID: conv_root" in text
    assert "Labels: a=1, b=2" in text
    assert "Messages: 2" in text
    assert "[1] type=tool_call_request name=Bash" in text
    # The output row picks its tool name up from the earlier request.
    assert "[2] type=tool_call_complete name=Bash" in text


async def test_overview_uses_sessions_api_session_id_and_shows_none_labels() -> None:
    client = _client(_Sessions(pages=[[]]))

    text = await _overview(None, client, _session(session_id="conv_s"))

    assert "Session ID: conv_s" in text
    assert "Labels: (none)" in text
    assert "Messages: 0" in text
    assert "(no messages yet)" in text


async def test_overview_uses_sidebar_target_key_for_main_and_sub_agents() -> None:
    main = await _overview(
        OverlayTarget(key="conv_main", label="main", icon="🤖"),
        _client(_Sessions(pages=[[]])),
        _session(),
    )
    child = await _overview(
        OverlayTarget(key="conv_child", label="coder:fix", icon="👾"),
        _client(_Sessions(pages=[[]])),
        _session(),
    )

    assert "Session ID: conv_main" in main and "Agent: coder" in main
    assert "Session: coder:fix" in child and "Session ID: conv_child" in child
    # Sub-agent panes don't repeat the main session's agent/model header.
    assert "Agent:" not in child


async def test_overview_reports_label_fetch_failure_but_still_lists_events() -> None:
    sessions = _Sessions(
        get_error=RuntimeError("labels down"),
        pages=[[{"type": "reasoning"}]],
    )

    text = await _overview(None, _client(sessions), _session(session_id="conv_s"))

    assert "Labels fetch failed: RuntimeError: labels down" in text
    assert "[1] type=reasoning" in text


async def test_overview_reports_item_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_items(client: object, conv_id: str) -> list[dict[str, object]]:
        raise RuntimeError("items down")

    monkeypatch.setattr(_repl, "_list_all_conversation_items", failing_items)

    text = await _overview(None, _client(), _session(session_id="conv_s"))

    assert "Failed to fetch conversation items: RuntimeError: items down" in text
    assert "Messages:" not in text
