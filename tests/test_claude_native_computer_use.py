"""Claude Code Computer Use classification and frame extraction.

Shapes here are taken from recorded Claude Code transcripts: computer use is an
MCP server, so calls arrive as ``mcp__computer-use__<method>``; results carry
images as base64 blocks and errors as a bare string with ``is_error``.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from omnigent.claude_native_bridge import (
    _claude_computer_use_frames,
    _claude_computer_use_presentation,
    _user_transcript_items_from_entry,
)
from omnigent.entities.conversation import ComputerUsePresentation


def test_computer_use_call_is_classified_with_claude_provider() -> None:
    """The MCP server name is the identity, so a bare screenshot still counts."""
    presentation = _claude_computer_use_presentation("mcp__computer-use__screenshot", {})
    assert presentation == {
        "kind": "computer_use",
        "provider": "claude",
        "action_kinds": ["inspect"],
    }
    ComputerUsePresentation.model_validate(presentation)


@pytest.mark.parametrize(
    "name",
    [
        # Browser surfaces drive a page, not the desktop. Claude names them with
        # their own MCP servers, and mislabeling them would fill the Computer
        # panel with browser automation.
        "mcp__Claude_Browser__computer",
        "mcp__claude-in-chrome__computer",
        # Ordinary tools must keep their generic tool card.
        "Read",
        "Bash",
    ],
)
def test_non_desktop_tools_are_not_classified(name: str) -> None:
    """Only the built-in computer-use server maps to the Computer panel."""
    assert _claude_computer_use_presentation(name, {"action": "screenshot"}) is None


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"app": "Simulator"}, "Simulator"),
        ({"apps": ["Calculator"], "reason": "inspect"}, "Calculator"),
    ],
)
def test_app_label_comes_from_the_calls_that_name_an_app(
    arguments: dict[str, object], expected: str
) -> None:
    """``open_application`` / ``request_access`` are the only app-naming calls."""
    presentation = _claude_computer_use_presentation(
        "mcp__computer-use__open_application", arguments
    )
    assert presentation is not None
    assert presentation["app_name"] == expected


def test_batch_summarizes_members_and_drops_the_routine_screenshot() -> None:
    """A batch's real work is in ``actions[].action``, not the tool name.

    Computer use refreshes state around each interaction, so a trailing
    screenshot alongside a click would otherwise read as mere inspection.
    """
    presentation = _claude_computer_use_presentation(
        "mcp__computer-use__computer_batch",
        {
            "actions": [
                {"action": "left_click", "coordinate": [821, 663]},
                {"action": "wait", "duration": 3},
                {"action": "screenshot"},
            ]
        },
    )
    assert presentation is not None
    assert presentation["action_kinds"] == ["click"]


def test_unknown_method_degrades_to_a_generic_action() -> None:
    """A newly shipped vendor method stays visible rather than vanishing."""
    presentation = _claude_computer_use_presentation("mcp__computer-use__teleport", {})
    assert presentation is not None
    assert presentation["action_kinds"] == ["interact"]


def test_frames_are_extracted_from_image_results() -> None:
    """Frames are read before the placeholder rewrite drops the base64."""
    block = {
        "type": "tool_result",
        "content": [
            {"type": "text", "text": "Screenshot captured."},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
            },
        ],
    }
    assert _claude_computer_use_frames(block) == (("image/jpeg", "QUJD"),)


@pytest.mark.parametrize(
    "block",
    [
        {"content": [{"type": "text", "text": 'Opened "Simulator".'}]},
        # A denied approval returns a bare string rather than content blocks.
        {"content": "Access to Simulator was not granted.", "is_error": True},
        {"content": [{"type": "image", "source": {"type": "url", "url": "http://x/y.png"}}]},
    ],
)
def test_results_without_inline_images_yield_no_frames(block: dict[str, object]) -> None:
    """Text-only, failed, and non-base64 results degrade to no frame."""
    assert _claude_computer_use_frames(block) == ()


def test_failed_tool_result_preserves_terminal_status_for_panel() -> None:
    """Claude's ``is_error`` flag must reach the Computer Use view model."""
    entry = {
        "uuid": "result-entry",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "Access to Simulator was not granted.",
                    "is_error": True,
                }
            ],
        },
    }

    _, items = _user_transcript_items_from_entry(
        entry,
        line_number=1,
        record_offset=0,
        agent_name="claude-native-ui",
        current_response_id="resp-1",
    )

    assert [item.data for item in items] == [
        {
            "call_id": "call-1",
            "output": "Access to Simulator was not granted.",
            "status": "failed",
        }
    ]


@pytest.mark.asyncio
async def test_known_app_carries_onto_calls_that_cannot_name_one() -> None:
    """A screenshot after opening an app keeps labeling that app.

    Only ``open_application`` / ``request_access`` name an app, so without this
    the panel flips to "Unknown app" on every screenshot of the app Claude is
    already driving.
    """
    from omnigent.claude_native_bridge import ClaudeTranscriptItem
    from omnigent.claude_native_forwarder import (
        _attach_computer_use_frames,
        _ForwardDedupeState,
    )

    def call(name: str, arguments: dict[str, object], call_id: str) -> ClaudeTranscriptItem:
        return ClaudeTranscriptItem(
            source_id=f"src-{call_id}",
            item_type="function_call",
            data={
                "name": name,
                "call_id": call_id,
                "presentation": _claude_computer_use_presentation(name, arguments),
            },
            response_id="resp",
        )

    dedupe = _ForwardDedupeState()
    opened = await _attach_computer_use_frames(
        None,
        session_id="s",
        item=call("mcp__computer-use__open_application", {"app": "Calculator"}, "c1"),
        dedupe=dedupe,
    )
    assert opened.data["presentation"]["app_name"] == "Calculator"
    shot = await _attach_computer_use_frames(
        None,
        session_id="s",
        item=call("mcp__computer-use__screenshot", {}, "c2"),
        dedupe=dedupe,
    )
    assert shot.data["presentation"]["app_name"] == "Calculator"


@pytest.mark.asyncio
async def test_classified_result_uploads_detected_frame_and_attaches_reference() -> None:
    """Only a known Computer Use result stores bytes, using signature MIME."""
    from omnigent.claude_native_bridge import ClaudeTranscriptItem
    from omnigent.claude_native_forwarder import (
        _attach_computer_use_frames,
        _ForwardDedupeState,
    )

    png = b"\x89PNG\r\n\x1a\nfixture"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "kind": "computer_frame",
                "file_id": "file-frame",
                "content_type": "image/png",
                "width": 1,
                "height": 1,
            },
        )

    dedupe = _ForwardDedupeState()
    call = ClaudeTranscriptItem(
        source_id="call-source",
        item_type="function_call",
        data={
            "call_id": "call-1",
            "presentation": {"kind": "computer_use", "provider": "claude"},
        },
        response_id="resp",
    )
    output = ClaudeTranscriptItem(
        source_id="output-source",
        item_type="function_call_output",
        data={"call_id": "call-1", "output": "captured"},
        response_id="resp",
        # Recorded Claude results can misdeclare image bytes, so detection wins.
        pending_frames=(("image/jpeg", base64.b64encode(png).decode()),),
    )

    async with httpx.AsyncClient(
        base_url="http://omnigent.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        await _attach_computer_use_frames(
            client,
            session_id="session-1",
            item=call,
            dedupe=dedupe,
        )
        attached = await _attach_computer_use_frames(
            client,
            session_id="session-1",
            item=output,
            dedupe=dedupe,
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/sessions/session-1/resources/computer-use-frames"
    assert request.url.params["source_id"] == "claude:output-source:0"
    assert b"image/png" in request.content
    assert png in request.content
    assert attached.data["attachments"] == [
        {
            "kind": "computer_frame",
            "file_id": "file-frame",
            "content_type": "image/png",
            "width": 1,
            "height": 1,
        }
    ]


@pytest.mark.asyncio
async def test_unclassified_image_result_never_uploads_a_frame() -> None:
    """An ordinary Claude image tool keeps its placeholder-only behavior."""
    from omnigent.claude_native_bridge import ClaudeTranscriptItem
    from omnigent.claude_native_forwarder import (
        _attach_computer_use_frames,
        _ForwardDedupeState,
    )

    output = ClaudeTranscriptItem(
        source_id="ordinary-image",
        item_type="function_call_output",
        data={"call_id": "ordinary-call", "output": "[image omitted]"},
        response_id="resp",
        pending_frames=(("image/png", base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()),),
    )

    def unexpected_upload(_request: httpx.Request) -> httpx.Response:
        pytest.fail("unclassified image result attempted a frame upload")

    async with httpx.AsyncClient(
        base_url="http://omnigent.test",
        transport=httpx.MockTransport(unexpected_upload),
    ) as client:
        unchanged = await _attach_computer_use_frames(
            client,
            session_id="session-1",
            item=output,
            dedupe=_ForwardDedupeState(),
        )

    assert unchanged is output
    assert "attachments" not in unchanged.data
