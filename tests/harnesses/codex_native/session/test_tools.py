"""Tools tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.harnesses.codex_native.session._support import (
    _capture_handler,
    _forwarder_context,
    _recording_forwarder_client,
    _write_forwarder_bridge,
)


def test_forwarder_posts_codex_turn_plan_update_only_to_tasks(tmp_path: Path) -> None:
    """Codex ``turn/plan/updated`` notifications update only the Tasks tab."""
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay one plan update notification.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "turn/plan/updated",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "explanation": None,
                        "plan": [{"step": "Inspect Codex plan events", "status": "pending"}],
                    },
                },
            )

    asyncio.run(run())

    assert [event["type"] for event in posted] == ["external_session_todos"]


def test_plan_todos_from_update_maps_steps_and_statuses() -> None:
    """
    ``_plan_todos_from_update`` maps Codex plan steps to the todo schema.

    Each step becomes ``{"content", "status", "activeForm"}`` with the
    status vocabulary normalized (``inProgress`` -> ``in_progress``) and the
    step text reused for ``activeForm`` since Codex has no gerund form.
    """
    todos = codex_native_forwarder._plan_todos_from_update(
        {
            "plan": [
                {"step": "Inspect", "status": "completed"},
                {"step": "Mirror", "status": "inProgress"},
                {"step": "Verify", "status": "in_progress"},
                {"step": "Ship", "status": "pending"},
                {"step": "Unknown", "status": "weird"},
            ]
        }
    )

    assert todos == [
        {"content": "Inspect", "status": "completed", "activeForm": "Inspect"},
        {"content": "Mirror", "status": "in_progress", "activeForm": "Mirror"},
        {"content": "Verify", "status": "in_progress", "activeForm": "Verify"},
        {"content": "Ship", "status": "pending", "activeForm": "Ship"},
        {"content": "Unknown", "status": "pending", "activeForm": "Unknown"},
    ]


def test_plan_todos_from_update_skips_malformed_and_empty() -> None:
    """
    ``_plan_todos_from_update`` drops malformed steps and empty plans.

    Non-dict entries and steps without a usable ``step`` string are
    skipped; a plan that is missing, not a list, or yields no valid
    items returns ``None`` so the caller posts nothing.
    """
    assert codex_native_forwarder._plan_todos_from_update({}) is None
    assert codex_native_forwarder._plan_todos_from_update({"plan": []}) is None
    assert codex_native_forwarder._plan_todos_from_update({"plan": "nope"}) is None
    assert (
        codex_native_forwarder._plan_todos_from_update(
            {"plan": ["bad", {"step": ""}, {"status": "pending"}]}
        )
        is None
    )
    assert codex_native_forwarder._plan_todos_from_update(
        {"plan": ["bad", {"step": "Keep me", "status": "pending"}]}
    ) == [{"content": "Keep me", "status": "pending", "activeForm": "Keep me"}]


def test_forwarder_posts_completed_codex_plan_item() -> None:
    """
    Completed Codex ``plan`` thread items are mirrored into Omnigent history.

    This covers resume/replay and final transcript state, where the
    plan arrives as a completed thread item rather than a live
    ``turn/plan/updated`` notification.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "plan",
                "id": "plan_123",
                "text": "1. Inspect\n2. Implement\n3. Verify",
            },
            _capture_handler(posted),
        )
    )

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "1. Inspect\n2. Implement\n3. Verify",
                        }
                    ],
                },
                "response_id": "codex_turn_123",
                "message_id": "codex:thread_123:turn_123:plan:plan_123",
                "source_id": "thread_123:turn_123:plan_123",
            },
        }
    ]


async def _replay_completed_item(
    item: dict[str, Any],
    handler: Callable[..., httpx.Response],
    *,
    bridge_dir: Path = Path("/tmp"),
) -> None:
    """
    Drive one Codex ``item/completed`` notification through the forwarder.

    :param item: Codex item payload, e.g. a ``commandExecution`` item.
    :param handler: MockTransport handler capturing the Omnigent posts.
    :returns: None.
    """
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    ) as client:
        await codex_native_forwarder._handle_event(
            client,
            **_forwarder_context(client, bridge_dir),
            event={
                "method": "item/completed",
                "params": {
                    "threadId": "thread_123",
                    "turnId": "turn_123",
                    "item": item,
                },
            },
        )


def test_forwarder_posts_codex_command_execution_tool_call() -> None:
    """
    A completed Codex ``commandExecution`` becomes a function-call pair.

    Native Codex sessions run Codex's own shell tool, so the single
    ``item/completed`` (which carries both the command and its
    aggregated output) must be mirrored as the Omnigent ``function_call`` /
    ``function_call_output`` pair the web UI renders. The item shape
    here matches a real app-server capture.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_abc123",
                "command": "/bin/zsh -lc 'cat hello.txt'",
                "cwd": "/repo",
                "status": "completed",
                "aggregatedOutput": "hello world\n",
                "exitCode": 0,
                "durationMs": 0,
            },
            _capture_handler(posted),
        )
    )

    # Both items must be posted: a function_call carrying the command as
    # arguments, then a function_call_output carrying the shell output.
    # A single post would mean the result was dropped; the call_id must
    # match across both so the UI pairs them into one tool card.
    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "codex-native-ui",
                    "name": "shell",
                    "arguments": '{"command": "/bin/zsh -lc \'cat hello.txt\'", "cwd": "/repo"}',
                    "call_id": "call_abc123",
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:call_abc123:call",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {
                    "call_id": "call_abc123",
                    "output": "hello world\n",
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:call_abc123:output",
            },
        },
    ]


def test_forwarder_streams_codex_command_output_before_completed_item(tmp_path: Path) -> None:
    """Command output deltas update the live tool before its final result."""
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []
    state = codex_native_forwarder._CodexForwarderState()
    started_item = {
        "type": "commandExecution",
        "id": "call_abc123",
        "command": "pytest -q",
        "cwd": "/repo",
        "status": "inProgress",
    }
    completed_item = {
        **started_item,
        "status": "completed",
        "aggregatedOutput": "collecting tests...\n1 passed\n",
        "exitCode": 0,
    }

    async def run() -> None:
        """Replay command start, output chunks, and completion."""
        async with _recording_forwarder_client(posted) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            events = [
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": started_item,
                    },
                },
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "itemId": "call_abc123",
                        "delta": "collecting ",
                    },
                },
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "itemId": "call_abc123",
                        "delta": "tests...",
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": completed_item,
                    },
                },
            ]
            for event in events:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    delta_coalescer=coalescer,
                    forwarder_state=state,
                )
            await coalescer.close()

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_conversation_item",
        "external_tool_output_delta",
        "external_conversation_item",
    ]
    assert posted[0]["data"]["item_type"] == "function_call"
    assert posted[1] == {
        "type": "external_tool_output_delta",
        "data": {"call_id": "call_abc123", "delta": "collecting tests..."},
    }
    assert posted[2]["data"] == {
        "item_type": "function_call_output",
        "item_data": {
            "call_id": "call_abc123",
            "output": "collecting tests...\n1 passed\n",
        },
        "response_id": "codex_turn_123",
        "source_id": "thread_123:turn_123:call_abc123:output",
    }


def test_forwarder_surfaces_failed_command_exit_code() -> None:
    """
    A non-zero command exit is surfaced in the mirrored output.

    Codex reports ``exitCode`` separately from ``aggregatedOutput``, so a
    failed command would look successful in the UI unless the forwarder
    folds the exit code into the output text.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_fail",
                "command": "/bin/zsh -lc 'exit 3'",
                "cwd": "/repo",
                "status": "failed",
                "aggregatedOutput": "boom\n",
                "exitCode": 3,
                "durationMs": 1,
            },
            _capture_handler(posted),
        )
    )

    outputs = [
        p["data"]["item_data"]["output"]
        for p in posted
        if p["data"]["item_type"] == "function_call_output"
    ]
    # The exit code (3, from the replayed item above) must appear appended
    # to the captured stderr/stdout. If the suffix were missing the output
    # would be just "boom\n" — a failed command indistinguishable from a
    # successful one in the UI.
    assert outputs == ["boom\n\n[exit code: 3]"]


def test_forwarder_posts_codex_file_change_tool_call() -> None:
    """
    A completed Codex ``fileChange`` becomes an apply_patch tool card.

    The item shape (``changes`` with ``path`` / ``kind`` / ``diff``)
    matches a real app-server capture; the forwarder must pass the
    changes through as arguments and summarize them as the output.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "fileChange",
                "id": "call_patch",
                "changes": [
                    {
                        "path": "/repo/greeting.py",
                        "kind": {"type": "add"},
                        "diff": "print('hi')\n",
                    }
                ],
                "status": "completed",
            },
            _capture_handler(posted),
        )
    )

    assert [p["data"]["item_type"] for p in posted] == [
        "function_call",
        "function_call_output",
    ]
    call = posted[0]["data"]["item_data"]
    assert call["name"] == "apply_patch"
    # Arguments carry the raw changes so the diff is recoverable in the UI.
    assert json.loads(call["arguments"]) == {
        "changes": [
            {"path": "/repo/greeting.py", "kind": {"type": "add"}, "diff": "print('hi')\n"}
        ]
    }
    # Output summarizes each change as "<kind> <path>" from real fields.
    assert posted[1]["data"]["item_data"]["output"] == "add /repo/greeting.py"


def test_forwarder_sends_file_change_to_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed fileChange reaches the non-git workspace registry relay."""
    (tmp_path / "tool_relay.json").write_text(
        json.dumps({"url": "http://relay.local", "token": "relay-secret"}),
        encoding="utf-8",
    )
    observed: list[dict[str, Any]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def urlopen(request: Any, *, timeout: float) -> Response:
        assert request.full_url == "http://relay.local/hook/observe-tool"
        assert request.headers["Authorization"] == "Bearer relay-secret"
        assert timeout == 2
        observed.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(codex_native_forwarder.urllib.request, "urlopen", urlopen)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/hook/observe-tool"
        return httpx.Response(202, json={"queued": False})

    asyncio.run(
        _replay_completed_item(
            {
                "type": "fileChange",
                "id": "call_patch",
                "changes": [
                    {"path": "/repo/new.py", "kind": {"type": "add"}, "diff": "new"},
                    {"path": "/repo/old.py", "kind": {"type": "delete"}, "diff": "old"},
                ],
                "status": "completed",
            },
            handler,
            bridge_dir=tmp_path,
        )
    )

    assert observed == [
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "changes": [
                    {"path": "/repo/new.py", "kind": {"type": "add"}},
                    {"path": "/repo/old.py", "kind": {"type": "delete"}},
                ]
            },
            "tool_response": {"type": "success"},
        }
    ]


@pytest.mark.parametrize("status", ["failed", "declined"])
def test_forwarder_does_not_observe_unsuccessful_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Failed or declined patches must not create phantom change records."""
    (tmp_path / "tool_relay.json").write_text(
        json.dumps({"url": "http://relay.local", "token": "relay-secret"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_native_forwarder.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("unsuccessful patch reached file observer"),
    )

    asyncio.run(
        _replay_completed_item(
            {
                "type": "fileChange",
                "id": f"call_patch_{status}",
                "changes": [{"path": "/repo/not-applied.py", "kind": {"type": "add"}}],
                "status": status,
            },
            lambda _request: httpx.Response(202, json={"queued": False}),
            bridge_dir=tmp_path,
        )
    )


def test_forwarder_posts_codex_web_search_tool_call() -> None:
    """
    A completed Codex ``webSearch`` becomes a web_search tool card.

    Codex does not surface search results, so the queries it ran are the
    only result data; the forwarder uses them as the output rather than
    inventing a result. The item shape matches a real app-server capture.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "webSearch",
                "id": "ws_123",
                "query": "python latest stable version",
                "action": {
                    "type": "search",
                    "query": "python latest stable version",
                    "queries": ["python latest stable version"],
                },
            },
            _capture_handler(posted),
        )
    )

    call = posted[0]["data"]["item_data"]
    assert call["name"] == "web_search"
    assert json.loads(call["arguments"]) == {"query": "python latest stable version"}
    assert posted[1]["data"]["item_data"]["output"] == "python latest stable version"


def test_forwarder_drops_codex_tool_item_missing_required_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A malformed tool item is dropped, not mirrored with invented fields.

    A ``commandExecution`` with no ``command`` must not be posted as a
    tool call with an empty/placeholder command, because that would
    create a misleading tool card. The forwarder logs and skips it.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_bad",
                "cwd": "/repo",
                "status": "completed",
                "aggregatedOutput": "",
                "exitCode": 0,
            },
            _capture_handler(posted),
        )
    )

    # Nothing is posted: a malformed item is dropped rather than mirrored
    # with a fabricated command, and the drop is logged for diagnosis.
    assert posted == []
    assert "Codex commandExecution missing command" in caplog.text


def test_forwarder_posts_codex_image_view_tool_call() -> None:
    """
    A completed Codex ``imageView`` becomes a view_image tool card.

    Codex emits an ``imageView`` item when the model opens a local image
    to look at it. Before this was handled the item was silently dropped,
    so the web transcript skipped a step the native TUI shows. The only
    datum is the path, so it is both the argument and the output (the web
    UI cannot fetch a runner-local path).
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "imageView",
                "id": "img_view_1",
                "path": "/repo/screenshot.png",
            },
            _capture_handler(posted),
        )
    )

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "codex-native-ui",
                    "name": "view_image",
                    "arguments": '{"path": "/repo/screenshot.png"}',
                    "call_id": "img_view_1",
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:img_view_1:call",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {
                    "call_id": "img_view_1",
                    "output": "/repo/screenshot.png",
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:img_view_1:output",
            },
        },
    ]


def test_forwarder_posts_codex_image_generation_tool_call() -> None:
    """
    A completed Codex ``imageGeneration`` becomes a generate_image tool card.

    The raw ``result`` (base64 image bytes) is deliberately NOT mirrored —
    the web UI has no assistant-side image rendering and the base64 blob
    would only bloat the transcript. The card carries the revised prompt as
    the argument and the status plus on-disk save path as the output.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "imageGeneration",
                "id": "img_gen_1",
                "status": "completed",
                "revisedPrompt": "a red bicycle on a beach",
                "result": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                "savedPath": "/repo/out.png",
            },
            _capture_handler(posted),
        )
    )

    assert [p["data"]["item_type"] for p in posted] == [
        "function_call",
        "function_call_output",
    ]
    call = posted[0]["data"]["item_data"]
    assert call["name"] == "generate_image"
    assert call["call_id"] == "img_gen_1"
    # The revised prompt is surfaced; the base64 result is never echoed.
    assert json.loads(call["arguments"]) == {"revised_prompt": "a red bicycle on a beach"}
    assert "iVBORw0KGgo" not in call["arguments"]
    output = posted[1]["data"]["item_data"]["output"]
    assert output == "status: completed\nsaved to /repo/out.png"
    assert "iVBORw0KGgo" not in output


def test_forwarder_drops_codex_image_generation_missing_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    An ``imageGeneration`` with no status is dropped, not mirrored blank.

    ``status`` is a required protocol field; its absence means a malformed
    item, which is logged and skipped rather than mirrored as an empty card.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "imageGeneration",
                "id": "img_gen_bad",
                "revisedPrompt": "x",
                "result": "y",
            },
            _capture_handler(posted),
        )
    )

    assert posted == []
    assert "Codex imageGeneration missing status" in caplog.text


def test_forwarder_posts_codex_entered_review_mode_marker() -> None:
    """
    Codex ``enteredReviewMode`` surfaces a visible review-mode marker.

    Codex ``/review`` brackets a turn with enter/exit thread items. The web
    UI has no review affordance, so the transition is mirrored as a short
    assistant-message marker (the same rail used for plan updates), carrying
    the review subject so the web user sees what is under review.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "enteredReviewMode",
                "id": "review_1",
                "review": "review the auth changes",
            },
            _capture_handler(posted),
        )
    )

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Entered review mode: review the auth changes",
                        }
                    ],
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:review_1",
            },
        }
    ]


def test_forwarder_posts_codex_exited_review_mode_marker() -> None:
    """
    Codex ``exitedReviewMode`` surfaces a visible exit marker.

    With no review subject the marker is just the header — a terse divider
    that tells the web user the session left review mode.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "exitedReviewMode",
                "id": "review_2",
                "review": "",
            },
            _capture_handler(posted),
        )
    )

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "Exited review mode"}],
                },
                "response_id": "codex_turn_123",
                "source_id": "thread_123:turn_123:review_2",
            },
        }
    ]


def test_command_execution_appends_sandbox_bypass_guidance_on_namespace_error() -> None:
    """A codex shell command that fails because codex's own command sandbox
    cannot start (no unprivileged user namespaces in a hardened container) gets
    actionable recovery guidance appended, instead of surfacing only the opaque
    ``bwrap: No permissions to create new namespace`` output (issue #657)."""
    item = {
        "command": "/bin/zsh -lc 'echo hi'",
        "aggregatedOutput": (
            "bwrap: No permissions to create new namespace, likely because the "
            "kernel does not allow non-privileged user namespaces.\n"
        ),
        "exitCode": 1,
    }
    tool_call = codex_native_forwarder._command_execution_tool_call("call_1", item)
    assert tool_call is not None
    # The raw bwrap output and the exit code are preserved verbatim...
    assert "No permissions to create new namespace" in tool_call.output
    assert "[exit code: 1]" in tool_call.output
    # ...with actionable recovery guidance appended (the "Full access" preset
    # and the config sandbox_mode workaround).
    assert "Full access" in tool_call.output
    assert "danger-full-access" in tool_call.output


def test_command_execution_leaves_normal_output_untouched() -> None:
    """A successful command keeps its output verbatim — the guidance only fires
    on the sandbox-namespace failure, never on ordinary output (issue #657)."""
    item = {"command": "pwd", "aggregatedOutput": "/repo\n", "exitCode": 0}
    tool_call = codex_native_forwarder._command_execution_tool_call("call_1", item)
    assert tool_call is not None
    assert tool_call.output == "/repo\n"
    assert "Full access" not in tool_call.output
    assert "danger-full-access" not in tool_call.output
