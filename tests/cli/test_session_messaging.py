"""``omnigent session send``, ``get`` and ``read`` against a fake server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
import respx
from click.testing import CliRunner, Result

from omnigent.cli import cli
from omnigent.session_create_cli import message_event

_BASE = "http://localhost:6767"
_SESSION = "conv_worker"
_STATE_URL = f"{_BASE}/v1/sessions/{_SESSION}"


def _message(item_id: str, role: str, text: str) -> dict:
    block = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [{"type": block, "text": text}],
        "created_at": 1795000000,
    }


def _shown(item: dict) -> dict:
    """What the CLI prints for *item*."""
    return {
        "id": item["id"],
        "role": item["role"],
        "text": item["content"][0]["text"],
        "created_at": item["created_at"],
    }


_TOOL_CALL = {"id": "call_1", "type": "function_call", "name": "shell", "arguments": "{}"}


@dataclass
class _FakeSession:
    """A session whose state and transcript move one step per state read."""

    items: list[dict] = field(
        default_factory=lambda: [
            _message("msg_1", "user", "Task"),
            _message("msg_2", "assistant", "Old answer"),
        ]
    )
    # Each state read serves the next snapshot and stores the items that arrived with it.
    states: list[tuple[dict, list[dict]]] = field(
        default_factory=lambda: [({"status": "idle"}, [])]
    )
    ack: tuple[int, dict] = (202, {"queued": True, "item_id": "msg_sent"})
    # A native terminal still starting holds the message as a pending input instead.
    stores_sent_message: bool = True
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _served: int = 0

    def install(self, router: respx.MockRouter) -> None:
        router.get(_STATE_URL).mock(side_effect=self._state)
        router.get(f"{_STATE_URL}/items").mock(side_effect=self._items)
        router.post(f"{_STATE_URL}/events").mock(side_effect=self._post)

    def _state(self, request: httpx.Request) -> httpx.Response:
        index = min(self._served, len(self.states) - 1)
        snapshot, arriving = self.states[index]
        if self._served < len(self.states):
            self.items.extend(arriving)
        self._served += 1
        self.calls.append(("state", dict(request.url.params)))
        return httpx.Response(200, json={"id": _SESSION, **snapshot})

    def _items(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append(("items", params))
        items = self.items
        if "after" in params:
            ids = [item["id"] for item in items]
            items = items[ids.index(params["after"]) + 1 :]
        if "type" in params:
            items = [item for item in items if item["type"] == params["type"]]
        if params.get("order") == "desc":
            items = list(reversed(items))
        limit = int(params.get("limit", 100))
        page = items[:limit]
        return httpx.Response(
            200,
            json={
                "data": page,
                "first_id": page[0]["id"] if page else None,
                "last_id": page[-1]["id"] if page else None,
                "has_more": len(items) > limit,
            },
        )

    def _post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(("post", body))
        status, ack = self.ack
        if status < 400 and ack.get("queued") and self.stores_sent_message:
            self.items.append(_message("msg_sent", "user", body["data"]["content"][0]["text"]))
        return httpx.Response(status, json=ack)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda server, configured: _BASE)
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None: None)
    # No local-server pidfile, so the printed URL has no --base-path prefix.
    monkeypatch.setattr("omnigent.host.local_server._read_local_server_pid_file", lambda: None)
    monkeypatch.setattr("omnigent.session_cli._WAIT_FIRST_INTERVAL_S", 0.001)
    monkeypatch.setattr("omnigent.session_cli._WAIT_MAX_INTERVAL_S", 0.005)
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


def _invoke(args: list[str], **kwargs: object) -> Result:
    return CliRunner().invoke(cli, ["session", *args], **kwargs)


def test_send_queues_the_message_and_exits_without_waiting(router: respx.MockRouter) -> None:
    session = _FakeSession()
    session.install(router)

    result = _invoke(["send", _SESSION, "--message-file", "-"], input="Add a test\n")

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    # The same event a web composer send posts, and nothing else is read.
    assert session.calls == [("post", message_event("Add a test\n"))]


def test_send_json_names_the_queued_message(router: respx.MockRouter) -> None:
    _FakeSession().install(router)

    result = _invoke(["send", _SESSION, "-m", "Add a test", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "session_id": _SESSION,
        "message_id": "msg_sent",
        "message_queued": True,
    }


@pytest.mark.parametrize(
    ("ack", "reason"),
    [
        (
            (409, {"error": {"message": "Session is archived"}}),
            "The server refused the message (409): Session is archived.",
        ),
        ((202, {"queued": False, "denied": True}), "A policy denied the message."),
        ((202, {"queued": False}), "The server did not queue the message."),
        ((404, {"error": {"message": "Not found"}}), f"Session {_SESSION} was not found."),
    ],
)
def test_send_reports_why_the_message_was_not_queued(
    router: respx.MockRouter, ack: tuple[int, dict], reason: str
) -> None:
    _FakeSession(ack=ack).install(router)

    result = _invoke(["send", _SESSION, "-m", "Add a test", "--wait"])

    assert result.exit_code == 1
    assert reason in result.stderr


def test_wait_prints_the_final_reply_once_the_turn_ends(router: respx.MockRouter) -> None:
    """A stale idle read before the turn starts must not end the wait."""
    looking = _message("msg_3", "assistant", "Looking at the tests")
    fixed = _message("msg_4", "assistant", "Fixed the flaky test")
    session = _FakeSession(
        states=[
            ({"status": "idle"}, []),
            ({"status": "running"}, [looking]),
            ({"status": "idle"}, [_TOOL_CALL, fixed]),
        ]
    )
    session.install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Fixed the flaky test\n"
    # The newest message is read before the send, so the old answer is never a reply.
    first_read, post = session.calls[0], session.calls[1]
    assert first_read == ("items", {"limit": "1", "order": "desc", "type": "message"})
    assert post[0] == "post"
    assert [kind for kind, _ in session.calls].count("state") == 3


def test_wait_json_lists_every_message_after_the_sent_one(router: respx.MockRouter) -> None:
    looking = _message("msg_3", "assistant", "Looking at the tests")
    fixed = _message("msg_4", "assistant", "Fixed the flaky test")
    _FakeSession(
        states=[({"status": "running"}, [looking]), ({"status": "idle"}, [_TOOL_CALL, fixed])]
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "session_id": _SESSION,
        "message_id": "msg_sent",
        "status": "idle",
        "pending_approvals": 0,
        "reply": "Fixed the flaky test",
        # Tool calls are left out.
        "messages": [_shown(looking), _shown(fixed)],
    }


def test_wait_holds_while_a_terminal_still_owes_the_message(router: respx.MockRouter) -> None:
    """A starting native terminal holds the message as a pending input with no id."""
    done = _message("msg_5", "assistant", "Done")
    session = _FakeSession(
        ack=(202, {"queued": True, "pending_id": "pending_1"}),
        stores_sent_message=False,
        states=[
            ({"status": "idle", "pending_inputs": [{"pending_id": "pending_1"}]}, []),
            # Stored now, but idle before its turn starts: still no reply.
            ({"status": "idle"}, [_message("msg_sent", "user", "Fix it")]),
            ({"status": "running"}, []),
            ({"status": "idle"}, [done]),
        ],
    )
    session.install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["message_id"] is None
    assert report["messages"] == [_shown(done)]


def test_wait_stops_when_the_session_waits_for_an_approval(router: respx.MockRouter) -> None:
    asking = _message("msg_3", "assistant", "I need to run the migration")
    _FakeSession(
        states=[({"status": "running", "pending_elicitations": [{"id": "el_1"}]}, [asking])]
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Ship it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert (report["status"], report["pending_approvals"]) == ("running", 1)
    assert report["reply"] == "I need to run the migration"
    assert "is waiting for you" in result.stderr


def test_wait_reports_a_failed_turn(router: respx.MockRouter) -> None:
    _FakeSession(states=[({"status": "failed"}, [])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "failed"
    assert "The turn failed" in result.stderr


def test_wait_gives_up_after_the_timeout(router: respx.MockRouter) -> None:
    _FakeSession(states=[({"status": "running"}, [])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--timeout", "0.05"])

    assert result.exit_code == 1
    assert "Timed out after 0.05s" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["-m", "x", "--message-file", "-"], "Pass --message or --message-file, not both."),
        ([], "Pass --message or --message-file."),
        (["-m", "  "], "The message is empty."),
        (["-m", "x", "--timeout", "5"], "--timeout only applies with --wait."),
    ],
)
def test_send_rejects_bad_arguments_before_any_request(
    router: respx.MockRouter, args: list[str], error: str
) -> None:
    session = _FakeSession()
    session.install(router)

    result = _invoke(["send", _SESSION, *args])

    assert result.exit_code == 2
    assert error in result.stderr
    assert session.calls == []


def _install_session_info(router: respx.MockRouter) -> respx.Route:
    route = router.get(_STATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": _SESSION,
                "status": "idle",
                "title": "Fix flaky test",
                "agent_id": "ag_1",
                "agent_name": "claude-native-ui",
                "runner_id": "runner_1",
                "host_id": "host_1",
                "model_override": "claude-sonnet-4-6",
                "permission_mode": "auto",
                "workspace": "/repo",
                "project_id": "proj_1",
                "updated_at": 1795000000,
                "pending_elicitations": [{"id": "el_1"}],
                "total_cost_usd": 1.25,
                "usage_by_model": {"model-a": {"input_tokens": 1200, "output_tokens": 40}},
            },
        )
    )
    router.get(f"{_BASE}/v1/runners/runner_1/status").mock(
        return_value=httpx.Response(200, json={"online": True})
    )
    router.get(f"{_BASE}/v1/hosts/host_1").mock(return_value=httpx.Response(404))
    router.get(f"{_BASE}/v1/projects/proj_1").mock(
        return_value=httpx.Response(200, json={"id": "proj_1", "name": "Flaky tests"})
    )
    return route


def test_get_json_is_what_sys_session_get_info_reports(router: respx.MockRouter) -> None:
    state = _install_session_info(router)

    result = _invoke(["get", _SESSION, "--usage", "--json"])

    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    assert state.calls.last.request.url.params["include_usage"] == "true"
    assert (info["session_id"], info["status"], info["title"]) == (
        _SESSION,
        "idle",
        "Fix flaky test",
    )
    # The agent tool's projection: the public agent name and the effective model.
    assert (info["agent_name"], info["model"]) == ("Claude", "claude-sonnet-4-6")
    assert (info["runner_online"], info["project_name"]) == (True, "Flaky tests")
    assert info["pending_elicitation_count"] == 1
    assert info["usage"]["total_cost_usd"] == 1.25
    assert info["usage"]["input_tokens"] == 1200
    assert info["url"] == f"{_BASE}/c/{_SESSION}"


def test_get_prints_a_readable_summary(router: respx.MockRouter) -> None:
    state = _install_session_info(router)

    with_usage = _invoke(["get", _SESSION, "--usage"])
    without_usage = _invoke(["get", _SESSION])

    assert with_usage.exit_code == 0, with_usage.output
    lines = with_usage.stdout.splitlines()
    assert lines[0] == _SESSION
    assert "status:          idle, 1 waiting for you" in lines
    assert "host:            host_1 (runner online)" in lines
    assert "project:         Flaky tests" in lines
    assert "cost:            $1.25" in lines
    assert "tokens:          1,200 input, 40 output" in lines
    assert f"url:             {_BASE}/c/{_SESSION}" in lines
    # Cost is opt-in: the default read skips the usage aggregate.
    assert state.calls.last.request.url.params["include_usage"] == "false"
    assert "cost:" not in without_usage.stdout


def test_get_reports_an_unknown_session(router: respx.MockRouter) -> None:
    router.get(_STATE_URL).mock(return_value=httpx.Response(404))

    result = _invoke(["get", _SESSION])

    assert result.exit_code == 1
    assert f"Session {_SESSION} was not found." in result.stderr


def _long_transcript() -> list[dict]:
    return [
        _message("msg_1", "user", "Task"),
        _TOOL_CALL,
        _message("msg_2", "assistant", "First pass"),
        _message("msg_3", "user", "Now add a test"),
        _message("msg_4", "assistant", "Added tests/test_x.py"),
    ]


def test_read_shows_the_newest_messages_oldest_first(router: respx.MockRouter) -> None:
    session = _FakeSession(items=_long_transcript())
    session.install(router)

    result = _invoke(["read", _SESSION, "--last", "2"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "── user · msg_3 ──\nNow add a test\n\n── assistant · msg_4 ──\nAdded tests/test_x.py\n"
    )
    assert session.calls == [("items", {"limit": "2", "order": "desc", "type": "message"})]


def test_read_defaults_to_the_last_ten_messages(router: respx.MockRouter) -> None:
    session = _FakeSession(items=_long_transcript())
    session.install(router)

    result = _invoke(["read", _SESSION, "--json"])

    assert result.exit_code == 0, result.output
    assert session.calls == [("items", {"limit": "10", "order": "desc", "type": "message"})]
    assert [message["id"] for message in json.loads(result.stdout)["messages"]] == [
        "msg_1",
        "msg_2",
        "msg_3",
        "msg_4",
    ]


def test_read_after_a_message_returns_only_newer_ones(router: respx.MockRouter) -> None:
    transcript = _long_transcript()
    session = _FakeSession(items=transcript)
    session.install(router)

    result = _invoke(["read", _SESSION, "--after", "msg_2", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "session_id": _SESSION,
        "messages": [_shown(transcript[3]), _shown(transcript[4])],
    }
    assert session.calls == [
        ("items", {"limit": "1000", "order": "asc", "type": "message", "after": "msg_2"})
    ]
