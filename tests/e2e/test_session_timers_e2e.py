"""Agent timers wake their session through the database scheduler, across restarts.

``sys_timer_set`` saves one ``kind="timer"`` scheduled task on the server, and
the server's scheduler delivers each firing into the same session as a hidden
``[System: timer <id> fired]`` message, waking the agent for a new turn. A mock
model calls the real tools; the server, runner, scheduler and tunnel are real
processes, restarted mid-flight. The server keeps its default 30 s poll, so a
firing that lands within seconds of its time shows the scheduler woke for it.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_session_timers_e2e.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.live_server import terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

pytestmark = pytest.mark.timeout(600, method="signal")

# Seconds a firing may trail its due time: the scheduler wakes for it instead of
# waiting out its 30 s poll, then wakes the runner and persists the message.
_FIRING_SLACK_S = 6.0
_REPLY = "Noted the timer."


@contextmanager
def _timer_stack(tmp_path: Path) -> Iterator[tuple[ServerRunner, httpx.Client]]:
    """A server and runner with a client that keeps background titles off the mock."""
    with (
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        stack.start_runner()
        yield stack, client


def _timer_session(client: httpx.Client, stack: ServerRunner, mock_url: str, model: str) -> str:
    """Create a runner-bound session whose agent has the timer tools."""
    spec = {
        "name": f"timer-agent-{model}",
        "prompt": "You set timers with sys_timer_set and cancel them with sys_timer_cancel.",
        "executor": {
            "harness": "openai-agents",
            "model": model,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_url}/v1"},
        },
        "timers": True,
    }
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({f"timer-agent-{model}.yaml": yaml.safe_dump(spec).encode()}),
    )
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(client.patch, "", session_id, stack.runner_id)
    return session_id


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """The session's transcript, each item flattened to its type plus its data."""
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return [
        {"type": item.get("type"), **(item.get("data") or {})}
        for item in response.json().get("items", [])
    ]


def _tool_output(client: httpx.Client, session_id: str, call_id: str) -> dict[str, Any]:
    """Wait for the runner's output for one scripted tool call and parse it."""
    found: list[dict[str, Any]] = []

    def output_recorded() -> bool:
        for item in _items(client, session_id):
            if item["type"] == "function_call_output" and item.get("call_id") == call_id:
                found.append(json.loads(item["output"]))
                return True
        return False

    poll_until(output_recorded, timeout_s=90, interval_s=0.25)
    return found[0]


def _firings(client: httpx.Client, session_id: str, timer_id: str) -> list[dict[str, Any]]:
    """The hidden firing messages one timer has delivered into the session."""
    marker = f"[System: timer {timer_id} fired]"
    return [
        item
        for item in _items(client, session_id)
        if item["type"] == "message"
        and item.get("role") == "user"
        and any(marker in (block.get("text") or "") for block in item.get("content", []))
    ]


def _restart_runner(stack: ServerRunner, client: httpx.Client) -> None:
    """Stop the runner process and start a new one with the same identity."""
    terminate_process(stack.runner)
    poll_until(
        lambda: not client.get(f"/v1/runners/{stack.runner_id}/status").json().get("online"),
        timeout_s=60,
    )
    stack.runner = None
    stack.start_runner()


def _set_timer(
    client: httpx.Client,
    mock_url: str,
    session_id: str,
    model: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Have the agent call ``sys_timer_set`` for one user turn; return the tool output."""
    call_id = f"call_set_{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "sys_timer_set",
                        "arguments": json.dumps(arguments),
                    }
                ]
            },
            {"text": "Timer set."},
        ],
        key=model,
        required_tools=["sys_timer_set"],
    )
    send_user_message_to_session(client, session_id=session_id, content="Set the timer.")
    return _tool_output(client, session_id, call_id)


def _model_saw(mock_url: str, model: str, text: str) -> bool:
    """Whether a captured model request carried *text* in its input."""
    return any(text in json.dumps(request) for request in get_mock_requests(mock_url, key=model))


def test_one_shot_timer_fires_once_after_runner_and_server_restarts(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """A one-shot timer set before both restarts still wakes the agent, exactly once."""
    mock_url = isolated_mock_llm_server_url
    model = f"mock-timer-once-{uuid.uuid4().hex[:6]}"
    set_fallback_mock_llm(mock_url, model, _REPLY)
    with _timer_stack(tmp_path) as (stack, client):
        session_id = _timer_session(client, stack, mock_url, model)
        timer = _set_timer(
            client, mock_url, session_id, model, {"seconds": 45, "note": "check the build"}
        )
        assert timer["status"] == "scheduled", timer
        timer_id = timer["timer_id"]
        fire_at = datetime.fromisoformat(timer["fire_at"]).timestamp()

        # One hidden scheduled task holds the timer; the Automations list never shows it.
        task = client.get(f"/v1/scheduled-tasks/{timer_id}").json()
        assert task["kind"] == "timer"
        assert task["session_id"] == session_id
        assert task["rrule"] == "FREQ=SECONDLY;INTERVAL=45;COUNT=1"
        assert datetime.fromisoformat(task["next_run_at"]).timestamp() == fire_at
        listed = client.get("/v1/scheduled-tasks").json()["scheduled_tasks"]
        assert timer_id not in {listed_task["id"] for listed_task in listed}

        _restart_runner(stack, client)
        stack.restart_server()
        assert time.time() < fire_at, "the restarts outlasted the timer; lengthen it"
        assert _firings(client, session_id, timer_id) == []

        poll_until(
            lambda: _firings(client, session_id, timer_id),
            timeout_s=fire_at - time.time() + 30,
            interval_s=0.2,
        )
        delivered_at = time.time()
        assert delivered_at - fire_at < _FIRING_SLACK_S, (
            f"the firing came {delivered_at - fire_at:.1f}s late\n{stack.log_tail()}"
        )
        (firing,) = _firings(client, session_id, timer_id)
        # The same hidden, note-carrying wake the runner's timers sent.
        assert firing["is_meta"] is True
        assert firing["content"] == [
            {
                "type": "input_text",
                "text": f"[System: timer {timer_id} fired]\nnote: 'check the build'",
            }
        ]
        # The firing woke the agent: the model received it and the agent answered.
        poll_until(lambda: _model_saw(mock_url, model, f"timer {timer_id} fired"), timeout_s=60)
        poll_until(
            lambda: any(
                item["type"] == "message"
                and item.get("role") == "assistant"
                and _REPLY in json.dumps(item.get("content"))
                for item in _items(client, session_id)
            ),
            timeout_s=60,
        )

        # A fired one-shot timer is spent: with its row gone nothing can claim it again.
        poll_until(
            lambda: client.get(f"/v1/scheduled-tasks/{timer_id}").status_code == 404,
            timeout_s=30,
        )
        assert len(_firings(client, session_id, timer_id)) == 1


def test_repeating_timer_keeps_its_id_and_stays_cancelled_across_restarts(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """Repeated ticks reuse one task, survive a runner restart, and stop for good on cancel."""
    mock_url = isolated_mock_llm_server_url
    model = f"mock-timer-repeat-{uuid.uuid4().hex[:6]}"
    interval = 6
    set_fallback_mock_llm(mock_url, model, _REPLY)
    with _timer_stack(tmp_path) as (stack, client):
        session_id = _timer_session(client, stack, mock_url, model)
        timer = _set_timer(
            client,
            mock_url,
            session_id,
            model,
            {"seconds": interval, "repeat": True, "note": "poll workers"},
        )
        assert timer["status"] == "scheduled", timer
        assert timer["interval_seconds"] == interval
        timer_id = timer["timer_id"]

        poll_until(lambda: len(_firings(client, session_id, timer_id)) >= 2, timeout_s=60)
        # Every tick is the same task: the task stays active with its next tick saved.
        task = client.get(f"/v1/scheduled-tasks/{timer_id}").json()
        assert (task["kind"], task["state"]) == ("timer", "active")
        assert task["rrule"] == f"FREQ=SECONDLY;INTERVAL={interval}"

        _restart_runner(stack, client)
        before_restart = len(_firings(client, session_id, timer_id))
        poll_until(
            lambda: len(_firings(client, session_id, timer_id)) > before_restart, timeout_s=60
        )

        # The agent cancels the timer from the turn its next tick starts.
        cancel_call = f"call_cancel_{uuid.uuid4().hex[:8]}"
        configure_mock_llm(
            mock_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": cancel_call,
                            "name": "sys_timer_cancel",
                            "arguments": json.dumps({"timer_id": timer_id}),
                        }
                    ]
                },
                {"text": "Timer cancelled."},
            ],
            key=model,
            required_tools=["sys_timer_cancel"],
        )
        assert _tool_output(client, session_id, cancel_call) == {
            "timer_id": timer_id,
            "status": "cancelled",
        }
        assert client.get(f"/v1/scheduled-tasks/{timer_id}").status_code == 404
        cancelled_count = len(_firings(client, session_id, timer_id))

        _restart_runner(stack, client)
        stack.restart_server()
        # Several intervals across both restarts: a cancelled timer never ticks again.
        time.sleep(4 * interval)
        assert len(_firings(client, session_id, timer_id)) == cancelled_count
        assert client.get(f"/v1/scheduled-tasks/{timer_id}").status_code == 404
