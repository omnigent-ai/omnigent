"""``omnigent session send``, ``get`` and ``read`` against a fake server."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import click
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


def _tool_call(item_id: str) -> dict:
    return {"id": item_id, "type": "function_call", "name": "shell", "arguments": "{}"}


def _error(item_id: str) -> dict:
    return {
        "id": item_id,
        "type": "error",
        "code": "host_launch_failed",
        "message": "host refused",
    }


def _shown(item: dict) -> dict:
    """What the CLI prints for *item*."""
    return {
        "id": item["id"],
        "role": item["role"],
        "text": item["content"][0]["text"],
        "created_at": item["created_at"],
    }


@dataclass
class _FakeSession:
    """A session whose state and transcript move one step per state read after the send."""

    items: list[dict] = field(
        default_factory=lambda: [
            _message("msg_1", "user", "Task"),
            _message("msg_2", "assistant", "Old answer"),
        ]
    )
    # What a state read returns before the message is posted.
    baseline: dict = field(default_factory=lambda: {"status": "idle"})
    # Each state read after the post serves the next snapshot. Its items arrive with
    # it, so the transcript read that follows already sees them.
    states: list[tuple[dict, list[dict]]] = field(
        default_factory=lambda: [({"status": "idle"}, [])]
    )
    ack: tuple[int, dict] = (202, {"queued": True, "item_id": "msg_sent"})
    # A native terminal still starting holds the message as a pending input instead.
    stores_sent_message: bool = True
    # Older servers ignore the items API's type filter.
    filters_by_type: bool = True
    state_delay_s: float = 0.0
    post_delay_s: float = 0.0
    calls: list[tuple[str, dict]] = field(default_factory=list)
    message_read_times: list[float] = field(default_factory=list)
    _posted: bool = False
    _served: int = 0

    def install(self, router: respx.MockRouter) -> None:
        router.get(_STATE_URL).mock(side_effect=self._state)
        router.get(f"{_STATE_URL}/items").mock(side_effect=self._items)
        router.post(f"{_STATE_URL}/events").mock(side_effect=self._post)

    async def _state(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(("state", dict(request.url.params)))
        await asyncio.sleep(self.state_delay_s)
        if not self._posted:
            return httpx.Response(200, json={"id": _SESSION, **self.baseline})
        index = min(self._served, len(self.states) - 1)
        snapshot, arriving = self.states[index]
        if self._served < len(self.states):
            self.items.extend(arriving)
        self._served += 1
        return httpx.Response(200, json={"id": _SESSION, **snapshot})

    def _items(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append(("items", params))
        if params.get("type") == "message" and params.get("order") == "asc":
            self.message_read_times.append(time.monotonic())
        items = list(reversed(self.items)) if params.get("order") == "desc" else self.items
        if "after" in params:
            ids = [item["id"] for item in items]
            items = items[ids.index(params["after"]) + 1 :]
        if "type" in params and self.filters_by_type:
            items = [item for item in items if item["type"] == params["type"]]
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

    async def _post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(("post", body))
        await asyncio.sleep(self.post_delay_s)
        self._posted = True
        status, ack = self.ack
        if status < 400 and ack.get("queued") and self.stores_sent_message:
            self.items.append(_message("msg_sent", "user", body["data"]["content"][0]["text"]))
        return httpx.Response(status, json=ack)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._resolve_attach_server", lambda server, configured: _BASE)
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None: None)
    monkeypatch.setattr("omnigent.session_cli._renew_stored_login", lambda server_url: None)
    # No local-server pidfile, so the printed URL has no --base-path prefix.
    monkeypatch.setattr("omnigent.host.local_server._read_local_server_pid_file", lambda: None)
    monkeypatch.setattr("omnigent.session_cli._WAIT_FIRST_INTERVAL_S", 0.001)
    monkeypatch.setattr("omnigent.session_cli._WAIT_MAX_INTERVAL_S", 0.005)
    monkeypatch.setattr("omnigent.session_cli._SETTLE_S", 0.002)
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


_LOOKING = _message("msg_3", "assistant", "Looking at the tests")
_FIXED = _message("msg_4", "assistant", "Fixed the flaky test")
# Idle reads that predate the turn: first before it starts, then with its progress
# message already written by the time the transcript is read.
_TURN_AFTER_STALE_IDLE_READS = [
    ({"status": "idle"}, []),
    ({"status": "idle"}, [_LOOKING]),
    ({"status": "running"}, []),
    ({"status": "idle"}, [_tool_call("call_1"), _FIXED]),
]


def test_wait_prints_the_final_reply_once_the_turn_ends(router: respx.MockRouter) -> None:
    session = _FakeSession(states=list(_TURN_AFTER_STALE_IDLE_READS))
    session.install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Fixed the flaky test\n"
    # The state and the newest message are read before the post, so the old
    # answer is never a reply.
    assert session.calls[:3] == [
        ("state", session.calls[0][1]),
        ("items", {"limit": "1", "order": "desc", "type": "message"}),
        ("post", message_event("Fix it")),
    ]


def test_wait_json_lists_every_message_after_the_sent_one(router: respx.MockRouter) -> None:
    _FakeSession(states=list(_TURN_AFTER_STALE_IDLE_READS)).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "session_id": _SESSION,
        "message_id": "msg_sent",
        "delivered": True,
        "status": "idle",
        "pending_approvals": 0,
        "reply": "Fixed the flaky test",
        # Tool calls are left out.
        "messages": [_shown(_LOOKING), _shown(_FIXED)],
    }


def test_wait_json_keeps_replies_when_another_sender_follows(router: respx.MockRouter) -> None:
    """Every message after the sent one is returned, not just those after the newest user one."""
    reply_a = _message("msg_3", "assistant", "Reply to the CLI")
    from_web = _message("msg_4", "user", "A message from the web")
    reply_b = _message("msg_5", "assistant", "Reply to the web")
    _FakeSession(
        states=[({"status": "running"}, [reply_a, from_web, reply_b]), ({"status": "idle"}, [])]
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["messages"] == [
        _shown(reply_a),
        _shown(from_web),
        _shown(reply_b),
    ]


def test_wait_ends_a_turn_that_leaves_no_reply(router: respx.MockRouter) -> None:
    """A turn stopped before any text, or one that only calls tools, still ends the wait."""
    _FakeSession(
        states=[({"status": "running"}, [_tool_call("call_1")]), ({"status": "idle"}, [])]
    ).install(router)

    plain = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert plain.exit_code == 0, plain.output
    assert plain.stdout == ""
    assert "turn ended without a reply" in plain.stderr


def test_wait_takes_a_reply_mirrored_after_the_turn_ends(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native terminal can report idle before its last reply reaches the transcript."""
    monkeypatch.setattr("omnigent.session_cli._SETTLE_S", 0.05)
    session = _FakeSession(
        states=[
            ({"status": "running"}, []),
            ({"status": "idle"}, [_LOOKING]),
            ({"status": "idle"}, [_FIXED]),
        ]
    )
    session.install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Fixed the flaky test\n"
    # Idle with the progress message, idle with the reply (the end moves), then
    # the same transcript again at least the settle period later.
    first, moved, confirmed = session.message_read_times[-3:]
    assert moved - first >= 0.05
    assert confirmed - moved >= 0.05


@pytest.mark.parametrize(
    "ack",
    [
        pytest.param({"queued": True, "item_id": "msg_sent"}, id="stored-id-not-visible-yet"),
        pytest.param({"queued": True, "pending_id": "pending_1"}, id="held-input-released"),
    ],
)
def test_wait_never_takes_another_senders_exchange_for_its_own(
    router: respx.MockRouter, ack: dict
) -> None:
    """Until the sent message shows up, nobody else's message stands in for it."""
    done = _message("msg_7", "assistant", "Done")
    _FakeSession(
        ack=(202, ack),
        stores_sent_message=False,
        states=[
            (
                {"status": "idle"},
                [
                    _message("msg_5", "user", "Other"),
                    _message("msg_6", "assistant", "Other reply"),
                ],
            ),
            ({"status": "idle"}, []),
            ({"status": "running"}, [_message("msg_sent", "user", "Fix  it\n")]),
            ({"status": "idle"}, [done]),
        ],
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["messages"] == [_shown(done)]


def test_wait_refuses_to_guess_between_identical_held_messages(router: respx.MockRouter) -> None:
    """Another sender's message with the same text can't be told apart from a held one."""
    held = {"pending_inputs": [{"pending_id": "pending_1"}]}
    _FakeSession(
        ack=(202, {"queued": True, "pending_id": "pending_1"}),
        stores_sent_message=False,
        states=[
            (
                {"status": "idle", **held},
                [
                    _message("msg_5", "user", "Fix it"),
                    _message("msg_6", "assistant", "Their reply"),
                ],
            ),
            ({"status": "running"}, [_message("msg_7", "user", "Fix it")]),
            ({"status": "idle"}, [_message("msg_8", "assistant", "Done")]),
        ],
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 1
    assert "more than one message with the text sent" in result.stderr
    assert result.stdout == ""


def test_wait_reports_a_held_message_the_session_never_delivered(
    router: respx.MockRouter,
) -> None:
    """A held input can fail to deliver without the session itself failing."""
    _FakeSession(
        ack=(202, {"queued": True, "pending_id": "pending_1"}),
        stores_sent_message=False,
        states=[
            ({"status": "idle", "failed_inputs": [{"pending_id": "pending_1", "content": []}]}, [])
        ],
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert (report["delivered"], report["status"]) == (False, "failed")
    assert "couldn't deliver the message" in result.stderr


@pytest.mark.parametrize("messages_before", [0, 25], ids=["first-page", "past-a-full-page"])
def test_wait_ends_a_tool_only_turn_that_never_read_as_running(
    router: respx.MockRouter, messages_before: int
) -> None:
    """A turn that only calls tools can start and end between two polls."""
    # Other people's messages can fill whole pages before the tool call.
    others = [_message(f"msg_o{n}", "user", f"Other {n}") for n in range(messages_before)]
    _FakeSession(states=[({"status": "idle"}, [*others, _tool_call("call_1")])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "turn ended without a reply" in result.stderr


def test_wait_returns_a_fast_turn_seen_only_by_its_reply(router: respx.MockRouter) -> None:
    """A turn that starts and ends between two polls never reads as running."""
    _FakeSession(states=[({"status": "idle"}, [_FIXED])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Fixed the flaky test\n"


def test_wait_holds_while_a_terminal_still_owes_the_message(router: respx.MockRouter) -> None:
    """A starting native terminal holds the message as a pending input with no id."""
    done = _message("msg_7", "assistant", "Done")
    pending = {"pending_inputs": [{"pending_id": "pending_1"}]}
    session = _FakeSession(
        ack=(202, {"queued": True, "pending_id": "pending_1"}),
        stores_sent_message=False,
        states=[
            # Someone else's exchange lands first: a reply the wait would take if
            # it ignored the input still waiting for the terminal.
            (
                {"status": "idle", **pending},
                [
                    _message("msg_5", "user", "Other"),
                    _message("msg_6", "assistant", "Other reply"),
                ],
            ),
            ({"status": "running", **pending}, []),
            ({"status": "idle", **pending}, []),
            ({"status": "running"}, [_message("msg_sent", "user", "Fix it")]),
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


_OLD_ERROR = {"code": "executor_error", "message": "rate limited"}


@pytest.mark.parametrize(
    ("baseline", "states"),
    [
        pytest.param({"status": "idle"}, [({"status": "failed"}, [])], id="fails-after-idle"),
        pytest.param(
            {"status": "failed", "last_task_error": _OLD_ERROR},
            [
                ({"status": "failed", "last_task_error": _OLD_ERROR}, []),
                ({"status": "running"}, []),
                ({"status": "failed", "last_task_error": _OLD_ERROR}, []),
            ],
            id="fails-again-after-running",
        ),
        pytest.param(
            {"status": "failed", "last_task_error": _OLD_ERROR},
            [
                (
                    {
                        "status": "failed",
                        "last_task_error": {"code": "executor_error", "message": "crashed"},
                    },
                    [],
                )
            ],
            id="fails-with-a-new-error",
        ),
        pytest.param(
            {"status": "failed", "last_task_error": _OLD_ERROR},
            [({"status": "failed", "last_task_error": _OLD_ERROR}, [_error("err_new")])],
            id="fails-to-launch-again-with-the-same-error",
        ),
    ],
)
def test_wait_reports_a_failed_turn(
    router: respx.MockRouter, baseline: dict, states: list[tuple[dict, list[dict]]]
) -> None:
    _FakeSession(baseline=baseline, states=states).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "failed"
    assert "The turn failed" in result.stderr


def test_wait_retrying_a_failed_session_ignores_its_old_failure(
    router: respx.MockRouter,
) -> None:
    """The last turn's failure stays on the session until the new turn starts."""
    old_failure = {"status": "failed", "last_task_error": _OLD_ERROR}
    _FakeSession(
        # The failed turn's error item follows its message, the newest one before the send.
        items=[_message("msg_1", "user", "Task"), _error("err_old")],
        baseline=old_failure,
        states=[(old_failure, []), ({"status": "running"}, []), ({"status": "idle"}, [_FIXED])],
    ).install(router)

    result = _invoke(["send", _SESSION, "-m", "Try again", "--wait"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "Fixed the flaky test\n"


def test_wait_gives_up_after_the_timeout(router: respx.MockRouter) -> None:
    _FakeSession(states=[({"status": "running"}, [])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--timeout", "0.05"])

    assert result.exit_code == 1
    assert "Timed out after 0.05s waiting for session conv_worker to reply" in result.stderr
    assert result.stdout == ""


def test_wait_timeout_bounds_slow_reads(router: respx.MockRouter) -> None:
    """A reply that would arrive after the deadline is not reported as a success."""
    _FakeSession(state_delay_s=0.1, states=[({"status": "idle"}, [_FIXED])]).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--timeout", "0.25"])

    assert result.exit_code == 1
    assert "Timed out after 0.25s waiting for session conv_worker to reply" in result.stderr


def test_wait_timeout_before_the_send_says_nothing_was_sent(router: respx.MockRouter) -> None:
    session = _FakeSession(state_delay_s=5.0)
    session.install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--timeout", "0.05"])

    assert result.exit_code == 1
    assert "reading session conv_worker; the message was not sent" in result.stderr
    assert [kind for kind, _ in session.calls] == ["state"]


def test_wait_timeout_covers_a_send_the_server_holds(router: respx.MockRouter) -> None:
    """An approval policy can hold the POST itself."""
    _FakeSession(post_delay_s=5.0).install(router)

    result = _invoke(["send", _SESSION, "-m", "Fix it", "--wait", "--timeout", "0.05"])

    assert result.exit_code == 1
    assert "Timed out after 0.05s before the server confirmed the message" in result.stderr


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


def test_session_client_renews_a_stored_login_before_it_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long wait outlives the stored login's access token through its refresh grant."""
    from omnigent.session_cli import open_session_client

    login = {"token": "old-token", "lapsing": False}
    refreshes: list[str] = []

    def load_token(server_url: str, *, min_remaining_seconds: float = 0.0) -> str | None:
        if login["lapsing"] and min_remaining_seconds > 0:
            return None
        return str(login["token"])

    def refresh_stored_token(server_url: str, *, timeout: float = 10.0) -> str | None:
        refreshes.append(server_url)
        login.update(token="new-token", lapsing=False)
        return "new-token"

    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", load_token)
    monkeypatch.setattr("omnigent.cli_auth.refresh_stored_token", refresh_stored_token)
    bearers: list[str | None] = []

    async def _requests() -> None:
        with respx.mock(assert_all_called=False) as mock_router:

            def _record(request: httpx.Request) -> httpx.Response:
                bearers.append(request.headers.get("Authorization"))
                return httpx.Response(200, json={"id": _SESSION, "status": "idle"})

            mock_router.get(_STATE_URL).mock(side_effect=_record)
            async with open_session_client(_BASE) as client:
                await client.get(f"/v1/sessions/{_SESSION}")
                login["lapsing"] = True
                await client.get(f"/v1/sessions/{_SESSION}")

    asyncio.run(_requests())

    assert bearers == ["Bearer old-token", "Bearer new-token"]
    assert refreshes == [_BASE]


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
    assert without_usage.exit_code == 0, without_usage.output
    assert state.calls.last.request.url.params["include_usage"] == "false"
    assert "status:" in without_usage.stdout
    assert "cost:" not in without_usage.stdout


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, f"Session {_SESSION} was not found."),
        (401, f"The server refused to show session {_SESSION}; check that you're signed in."),
    ],
)
def test_get_reports_a_session_it_cannot_show(
    router: respx.MockRouter, status: int, error: str
) -> None:
    router.get(_STATE_URL).mock(return_value=httpx.Response(status))

    result = _invoke(["get", _SESSION])

    assert result.exit_code == 1
    assert error in result.stderr


def test_session_command_errors_skip_the_stale_host_hint(router: respx.MockRouter) -> None:
    """`omnigent stop` can't fix a missing session, so the CLI shouldn't suggest it."""
    from omnigent.cli_diagnostics import suppresses_recovery_hint
    from omnigent.session_cli import open_session_client, session_info

    router.get(_STATE_URL).mock(return_value=httpx.Response(404))

    async def _get() -> None:
        async with open_session_client(_BASE) as client:
            await session_info(client, _SESSION, include_usage=False)

    with pytest.raises(click.ClickException) as raised:
        asyncio.run(_get())
    assert suppresses_recovery_hint(raised.value)


def _transcript(count: int) -> list[dict]:
    """*count* messages, alternating user and assistant, with a tool call after each reply."""
    items: list[dict] = []
    for number in range(1, count + 1):
        role = "user" if number % 2 else "assistant"
        items.append(_message(f"msg_{number}", role, f"Message {number}"))
        if role == "assistant":
            items.append(_tool_call(f"call_{number}"))
    return items


def _ids(result: Result) -> list[str]:
    return [message["id"] for message in json.loads(result.stdout)["messages"]]


def test_read_shows_the_newest_messages_oldest_first(router: respx.MockRouter) -> None:
    session = _FakeSession(items=_transcript(4))
    session.install(router)

    result = _invoke(["read", _SESSION, "--last", "2"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "── user · msg_3 ──\nMessage 3\n\n── assistant · msg_4 ──\nMessage 4\n"
    assert session.calls == [("items", {"limit": "2", "order": "desc", "type": "message"})]


def test_read_defaults_to_the_last_ten_messages(router: respx.MockRouter) -> None:
    session = _FakeSession(items=_transcript(12))
    session.install(router)

    result = _invoke(["read", _SESSION, "--json"])

    assert result.exit_code == 0, result.output
    assert _ids(result) == [f"msg_{number}" for number in range(3, 13)]
    assert session.calls == [("items", {"limit": "10", "order": "desc", "type": "message"})]


def test_read_pages_past_tool_calls_on_a_server_without_the_message_filter(
    router: respx.MockRouter,
) -> None:
    items = [*_transcript(2), _tool_call("call_a"), _tool_call("call_b"), _tool_call("call_c")]
    session = _FakeSession(items=items, filters_by_type=False)
    session.install(router)

    result = _invoke(["read", _SESSION, "--last", "2", "--json"])

    assert result.exit_code == 0, result.output
    assert _ids(result) == ["msg_1", "msg_2"]
    assert [params for _, params in session.calls] == [
        {"limit": "2", "order": "desc", "type": "message"},
        {"limit": "1000", "order": "desc", "type": "message", "after": "call_b"},
    ]


@pytest.mark.parametrize("filters_by_type", [True, False], ids=["filtered", "older-server"])
def test_read_after_a_message_follows_every_page(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, filters_by_type: bool
) -> None:
    monkeypatch.setattr("omnigent.session_cli._ITEMS_PAGE_LIMIT", 2)
    session = _FakeSession(items=_transcript(7), filters_by_type=filters_by_type)
    session.install(router)

    everything = _invoke(["read", _SESSION, "--after", "msg_2", "--json"])
    newest = _invoke(["read", _SESSION, "--after", "msg_2", "--last", "2", "--json"])

    assert everything.exit_code == 0, everything.output
    assert _ids(everything) == ["msg_3", "msg_4", "msg_5", "msg_6", "msg_7"]
    assert newest.exit_code == 0, newest.output
    assert _ids(newest) == ["msg_6", "msg_7"]
    assert session.calls[0] == (
        "items",
        {"limit": "2", "order": "asc", "type": "message", "after": "msg_2"},
    )
