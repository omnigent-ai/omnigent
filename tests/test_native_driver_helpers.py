"""Failure-path checks for reusable native journey observations."""

import json

import httpx
import pytest

from tests.e2e_ui.native_driver import (
    DriverSetupError,
    LogWindow,
    inject_child_start,
    send_message,
    wait_claude_completion,
    wait_native_delegation,
)


def test_rejected_message_fails_before_any_wait():
    def respond(request):
        body = json.loads(request.content)
        assert body["data"]["content"] == [{"type": "input_text", "text": "hello"}]
        return httpx.Response(400, json={"error": "invalid event"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DriverSetupError, match="400"):
            send_message(client, "session", "hello")


@pytest.mark.parametrize("response", [{"queued": False}, {"queued": True}])
def test_message_requires_accepted_identity(response):
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    ) as client:
        with pytest.raises(DriverSetupError, match="accepted input id"):
            send_message(client, "session", "hello")


def test_typed_synthetic_child_requires_returned_link():
    def respond(request):
        body = json.loads(request.content)
        assert body["type"] == "external_subagent_start"
        assert body["data"]["tool_use_id"] == "call"
        return httpx.Response(202, json={"child_session_id": "child"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        assert (
            inject_child_start(
                client,
                "parent",
                subagent_id="agent",
                agent_type="researcher",
                description="inspect",
                tool_use_id="call",
            )
            == "child"
        )


@pytest.mark.parametrize("missing", ["invocation", "result", "child", None])
def test_native_proof_requires_exact_call_and_link(missing):
    items = [
        {"type": "function_call", "name": "Agent", "call_id": "call"},
        {"type": "function_call_output", "call_id": "call", "output": "done"},
    ]
    child = {
        "id": "child",
        "parent_session_id": "parent",
        # The child-summary endpoint no longer exposes task identity.
        "current_task_id": None,
        "current_task_status": "completed",
        "busy": False,
        "labels": {"omnigent.claude_native.tool_use_id": "call"},
    }
    if missing == "invocation":
        items[0]["call_id"] = "earlier-call"
    if missing == "result":
        items[1]["call_id"] = "earlier-call"
    if missing == "child":
        child["labels"]["omnigent.claude_native.tool_use_id"] = "earlier-call"

    def respond(request):
        return httpx.Response(
            200, json={"data": [child] if request.url.path.endswith("child_sessions") else items}
        )

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        if missing:
            with pytest.raises(AssertionError, match="not observed"):
                wait_native_delegation(
                    client, "parent", call_id="call", tool_name="Agent", timeout=0
                )
        else:
            assert (
                wait_native_delegation(
                    client, "parent", call_id="call", tool_name="Agent", timeout=0
                ).child_id
                == "child"
            )


def test_logs_require_attempt_window_and_all_exact_identifiers(tmp_path):
    path = tmp_path / "runner.log"
    path.write_text("session=child turn=turn old warning\n")
    window = LogWindow.begin(path)
    with path.open("a") as handle:
        handle.write(
            "session=child-other turn=turn warning\n"
            "session=child turn=turn-old warning\n"
            "session=child turn=turn warning\n"
            "agent=general-purpose unrelated warning\n"
        )
    assert window.finish(session_id="child", turn_id="turn") == ["session=child turn=turn warning"]
    path.write_text("")
    with pytest.raises(AssertionError, match="truncated"):
        window.finish(session_id="child")


@pytest.mark.parametrize(
    "event_type,session,text,expected",
    [
        ("message", "parent", "prompt", True),
        ("interrupt", "parent", "prompt", False),
        ("function_call_output", "parent", "prompt", False),
        ("message", "other", "prompt", False),
        ("message", "parent", "different prompt", False),
    ],
)
def test_composer_observation_matches_its_own_submission(event_type, session, text, expected):
    from types import SimpleNamespace

    from tests.e2e_ui.native_driver import _matches_message_response

    response = SimpleNamespace(
        url=f"http://server/v1/sessions/{session}/events",
        request=SimpleNamespace(
            method="POST",
            post_data_json={
                "type": event_type,
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        ),
    )
    assert _matches_message_response(response, "parent", "prompt") is expected


@pytest.mark.parametrize("placement", ["tool_result", "text_block", "text_message"])
@pytest.mark.parametrize("status", ["completed", "running", "failed", "cancelled"])
def test_native_notification_requires_completed_exact_call(placement, status):
    notification = (
        "<task-notification><tool-use-id>call</tool-use-id>"
        f"<status>{status}</status><result>worker reply</result></task-notification>"
    )
    if placement == "tool_result":
        content = [{"type": "tool_result", "tool_use_id": "call", "content": notification}]
    elif placement == "text_block":
        content = [{"type": "text", "text": notification}]
    else:
        content = notification
    requests = [{"messages": [{"role": "user", "content": content}]}]
    with httpx.Client(
        base_url="http://mock",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"requests": requests})),
    ) as client:
        with pytest.raises(AssertionError, match="did not receive completion"):
            wait_claude_completion(
                client, call_id="other", expected_text="worker reply", timeout=0
            )
        if status == "completed":
            assert (
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )["kind"]
                == "notification"
            )
        else:
            with pytest.raises(AssertionError):
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("reply", ["worker reply", [{"type": "text", "text": "worker reply"}]])
def test_native_synchronous_completion_rejects_errors(is_error, reply):
    requests = [
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call",
                            "content": reply,
                            "is_error": is_error,
                        }
                    ],
                }
            ]
        }
    ]
    with httpx.Client(
        base_url="http://mock",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"requests": requests})),
    ) as client:
        if is_error:
            with pytest.raises(AssertionError, match="returned an error"):
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )
        else:
            assert (
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )["kind"]
                == "tool_result"
            )


@pytest.mark.parametrize(
    "text",
    [
        "worker reply",
        "<task-notification><result>worker reply</result></task-notification>",
        "".join(
            (
                "<task-notification><tool-use-id>call</tool-use-id><status>completed</status>",
                "<result>worker reply</task-notification>",
            )
        ),
    ],
)
def test_plain_or_malformed_notification_is_not_completion(text):
    requests = [{"messages": [{"role": "user", "content": text}]}]
    with httpx.Client(
        base_url="http://mock",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"requests": requests})),
    ) as client:
        with pytest.raises(AssertionError, match="did not receive completion"):
            wait_claude_completion(client, call_id="call", expected_text="worker reply", timeout=0)


@pytest.mark.parametrize(
    "notification",
    [
        "".join(
            (
                "<task-notification><tool-use-id>other</tool-use-id><status>completed</status>",
                "<result>worker reply</result></task-notification>",
            )
        ),
        "<task-notification><result>unescaped & content</result></task-notification>",
    ],
)
@pytest.mark.parametrize("reply", ["worker reply", "still waiting"])
def test_synchronous_reply_outside_unrelated_notification(notification, reply):
    requests = [
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call",
                            "content": reply + notification,
                        }
                    ],
                }
            ]
        }
    ]
    with httpx.Client(
        base_url="http://mock",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"requests": requests})),
    ) as client:
        if reply == "worker reply":
            assert (
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )["kind"]
                == "tool_result"
            )
        else:
            with pytest.raises(AssertionError, match="did not receive completion"):
                wait_claude_completion(
                    client, call_id="call", expected_text="worker reply", timeout=0
                )
