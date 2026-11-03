"""Tests for the Kiro-native ACP permission mirror."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.harnesses.kiro_native import permissions as knp
from omnigent.harnesses.kiro_native.bridge import acp_record_path
from omnigent.harnesses.kiro_native.permissions import (
    kiro_permission_elicitation_id,
    parse_permission_request,
)
from tests.budgets import budget


def _permission_msg(
    request_id: str = "req-1",
    *,
    allow_option_id: str = "allow_once",
    reject_option_id: str = "reject_once",
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "kiro-session",
            "toolCall": {"toolCallId": f"tool-{request_id}", "title": "Running: pwd"},
            "options": [
                {"optionId": allow_option_id, "name": "Yes", "kind": "allow_once"},
                {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                {"optionId": reject_option_id, "name": "No", "kind": "reject_once"},
            ],
            "_meta": {"trustOptions": True},
        },
    }


def _permission_result_msg(request_id: str = "req-1", option_id: str = "allow_once") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
    }


def _record(message: dict, *, direction: str = "out") -> dict:
    return {"ts": "2026-06-25T00:00:00Z", "dir": direction, "msg": json.dumps(message)}


def _record_bytes(message: dict, *, direction: str = "out") -> bytes:
    return (json.dumps(_record(message, direction=direction)) + "\n").encode("utf-8")


def test_parse_permission_request_extracts_one_time_options() -> None:
    req = parse_permission_request(_permission_msg())

    assert req is not None
    assert req.request_id == "req-1"
    assert req.tool_call_id == "tool-req-1"
    assert req.title == "Running: pwd"
    assert req.accept_option_id == "allow_once"
    assert req.decline_option_id == "reject_once"
    assert req.preview == "Running: pwd"


def test_parse_permission_request_preserves_option_ids_by_kind() -> None:
    req = parse_permission_request(
        _permission_msg("req-1", allow_option_id="yes-1", reject_option_id="no-1")
    )

    assert req is not None
    assert req.accept_option_id == "yes-1"
    assert req.decline_option_id == "no-1"


def test_parse_write_permission_uses_canonical_path(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    message = _permission_msg()
    message["params"]["toolCall"].update(
        title="Creating check.txt", rawInput={"path": str(alias / "check.txt")}
    )
    message["params"]["_meta"] = {"trustOptions": [{"setting_key": "runtime_write_paths"}]}
    permission = parse_permission_request(message)
    assert permission is not None
    assert permission.terminal_title == f"Write {actual.resolve() / 'check.txt'}"


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"method": "session/prompt"}, id="not-permission"),
        pytest.param({**_permission_msg(), "id": ""}, id="missing-id"),
        pytest.param(
            {
                **_permission_msg(),
                "params": {**_permission_msg()["params"], "toolCall": {"title": "Running: pwd"}},
            },
            id="missing-tool-call-id",
        ),
        pytest.param(
            {
                **_permission_msg(),
                "params": {
                    **_permission_msg()["params"],
                    "toolCall": {"toolCallId": "tool-1"},
                },
            },
            id="missing-title",
        ),
        pytest.param(
            {**_permission_msg(), "params": {**_permission_msg()["params"], "options": []}},
            id="missing-one-time-options",
        ),
    ],
)
def test_parse_permission_request_returns_none_for_unsupported_shapes(message: dict) -> None:
    assert parse_permission_request(message) is None


def test_permission_result_request_id_extraction() -> None:
    assert knp._permission_result_request_id(_permission_result_msg("req-9")) == "req-9"
    assert knp._permission_result_request_id({"id": "req-9", "result": {}}) is None
    assert knp._permission_result_request_id({"id": 1, "result": {"outcome": {}}}) is None


def test_elicitation_id_is_deterministic_and_session_scoped() -> None:
    eid = kiro_permission_elicitation_id("conv_abc", "req-1")
    assert eid == kiro_permission_elicitation_id("conv_abc", "req-1")
    assert eid != kiro_permission_elicitation_id("conv_other", "req-1")
    assert eid.startswith("elicit_kiro_conv_abc_")


def test_read_new_permission_events_incremental_and_partial_line(tmp_path: Path) -> None:
    record_file = tmp_path / "kiro_acp_record.jsonl"
    record_file.write_bytes(_record_bytes(_permission_msg("req-1")))

    events, offset = knp._read_new_permission_events(record_file, 0)

    assert [(event.kind, event.request_id) for event in events] == [("request", "req-1")]

    with record_file.open("ab") as handle:
        handle.write(_record_bytes(_permission_result_msg("req-1"), direction="in"))
        handle.write(b'{"dir":"out","msg":"')

    events2, offset2 = knp._read_new_permission_events(record_file, offset)

    assert [(event.kind, event.request_id) for event in events2] == [("response", "req-1")]
    assert offset2 == offset + len(_record_bytes(_permission_result_msg("req-1"), direction="in"))


def test_read_new_permission_events_ignores_malformed_and_non_permission(tmp_path: Path) -> None:
    record_file = tmp_path / "kiro_acp_record.jsonl"
    record_file.write_bytes(
        b"not-json\n"
        + _record_bytes({"jsonrpc": "2.0", "method": "session/prompt"})
        + _record_bytes(_permission_msg("req-1"))
    )

    events, _offset = knp._read_new_permission_events(record_file, 0)

    assert [(event.kind, event.request_id) for event in events] == [("request", "req-1")]


class _QueueClient:
    """Async httpx-client stub: records POSTs, returns queued responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
        self.posts.append((url, json))
        return self._responses.pop(0)


@pytest.mark.parametrize(
    ("response", "expected_action"),
    [
        pytest.param(httpx.Response(200, json={"action": "accept"}), "accept", id="accept"),
        pytest.param(httpx.Response(200, json={"action": "decline"}), "decline", id="decline"),
        pytest.param(httpx.Response(200, json={"action": "cancel"}), "cancel", id="cancel"),
        pytest.param(httpx.Response(400, text="nope"), None, id="rejected"),
        pytest.param(httpx.Response(200, content=b"not-json"), None, id="non-json"),
    ],
)
@pytest.mark.asyncio
async def test_run_one_permission_posts_then_delivers_verdict(
    response: httpx.Response,
    expected_action: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[tuple[Path, str]] = []

    def _fake_send(
        bridge_dir: Path, *, action: str, expected_title: str | None = None, **kwargs: object
    ) -> None:
        assert expected_title == "Running: pwd"
        delivered.append((bridge_dir, action))
        acp_record_path(bridge_dir).write_bytes(_record_bytes(_permission_result_msg()))

    monkeypatch.setattr(knp, "send_kiro_permission_verdict", _fake_send)
    req = parse_permission_request(_permission_msg("req-1"))
    assert req is not None
    client = _QueueClient([response, httpx.Response(200)])

    await knp._run_one_permission(
        client,  # type: ignore[arg-type]
        session_id="conv_1",
        bridge_dir=tmp_path,
        permission=req,
        elicitation_id="elic_1",
        record_offset=0,
    )

    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/hooks/native-permission-request"
    delivery = body.pop("native_delivery")
    assert delivery["state"] == "ready"
    assert delivery["attempt"]
    assert body == {
        "elicitation_id": "elic_1",
        "agent": "Kiro",
        "policy_name": "kiro_native_permission",
        "operation_type": "tool",
        "message": "Kiro wants approval for Running: pwd",
        "content_preview": "Running: pwd",
    }
    if expected_action is None:
        assert delivered == []
    else:
        assert delivered == [(tmp_path, expected_action)]


@pytest.mark.asyncio
async def test_post_external_elicitation_resolved_shape() -> None:
    client = _QueueClient([httpx.Response(200)])
    await knp._post_external_elicitation_resolved(client, "conv_2", "elic_9")  # type: ignore[arg-type]
    assert client.posts == [
        (
            "/v1/sessions/conv_2/events",
            {"type": "external_elicitation_resolved", "data": {"elicitation_id": "elic_9"}},
        )
    ]


@pytest.mark.parametrize(
    ("action", "path", "trusted_write", "keys"),
    [
        ("accept", "/workspace/kiro-approval-check.txt", True, ["Enter"]),
        ("decline", "/workspace/kiro-approval-check.txt", True, ["Down", "Down", "Enter"]),
        ("accept", "/other/kiro-approval-check.txt", True, []),
        ("accept", "/workspace/kiro-approval-check.txt", False, []),
        ("accept", "kiro-approval-check.txt", True, []),
    ],
)
async def test_write_permission_matches_recorded_path_before_delivering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action, path, trusted_write, keys
) -> None:
    from omnigent.harnesses.kiro_native import bridge
    from tests.test_kiro_native_bridge import _WRITE_PERMISSION_PANE, _install_fake_tmux

    monkeypatch.setattr(knp, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.1)
    for setting in (
        "_PERMISSION_ENTER_SETTLE_S",
        "_PERMISSION_KEY_INTERVAL_S",
        "_POLL_INTERVAL_S",
    ):
        monkeypatch.setattr(bridge, setting, 0)
    rejected = _WRITE_PERMISSION_PANE.replace(
        "❯ Yes, single permission", "  Yes, single permission"
    ).replace("  No (Tab to edit)", "❯ No (Tab to edit)")
    calls = _install_fake_tmux(
        monkeypatch, pane_outputs=[_WRITE_PERMISSION_PANE, _WRITE_PERMISSION_PANE, rejected]
    )
    bridge.write_tmux_target(tmp_path, socket_path=Path("/tmp/tmux.sock"), tmux_target="main")
    message = _permission_msg()
    message["params"]["toolCall"].update(
        title="Creating kiro-approval-check.txt",
        rawInput={"command": "create", "path": path, "content": "hello from Kiro"},
    )
    if trusted_write:
        message["params"]["_meta"] = {
            "trustOptions": [{"setting_key": "runtime_write_paths", "patterns": [path]}]
        }
    permission = parse_permission_request(message)
    assert permission is not None
    client = _QueueClient([httpx.Response(200, json={"action": action})])
    await knp._run_one_permission(
        client,  # type: ignore[arg-type]
        session_id="conv_write",
        bridge_dir=tmp_path,
        permission=permission,
        elicitation_id="elic_write",
        record_offset=0,
    )
    assert [call[-1] for call in calls if "send-keys" in call] == keys
    assert client.posts[0][1]["content_preview"] == "Creating kiro-approval-check.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", [202, 403])
async def test_confirmation_backs_off_during_an_outage(
    monkeypatch: pytest.MonkeyPatch,
    final_status: int,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(knp, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("offline", request=request)
        if len(requests) <= 10:
            return httpx.Response(429 if len(requests) % 2 else 503)
        return httpx.Response(final_status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        await knp._post_external_elicitation_resolved(client, "conv_2", "elic_9")

    assert len(requests) == 11
    assert all(request.content == requests[0].content for request in requests)
    delays = [call.args[0] for call in sleep.await_args_list]
    assert len(delays) == 10
    for delay, ceiling in zip(
        delays, [0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 30, 30, 30], strict=True
    ):
        assert ceiling / 2 <= delay <= ceiling


@pytest.mark.asyncio
async def test_expired_hook_wait_reconnects_without_sending_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    attempts: list[str] = []

    def send(bridge_dir: Path, *, action: str, **_kwargs: object) -> None:
        sent.append(action)
        acp_record_path(bridge_dir).write_bytes(_record_bytes(_permission_result_msg()))

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(202)
        attempts.append(json.loads(request.content)["native_delivery"]["attempt"])
        assert sent == []
        if len(attempts) == 1:
            return httpx.Response(200)
        return httpx.Response(200, json={"action": "accept"})

    monkeypatch.setattr(knp, "send_kiro_permission_verdict", send)
    monkeypatch.setattr(knp, "_POLL_INTERVAL_S", 0.01)
    permission = parse_permission_request(_permission_msg())
    assert permission is not None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        await knp._run_one_permission(
            client,
            session_id="conv_1",
            bridge_dir=tmp_path,
            permission=permission,
            elicitation_id="elic_1",
            record_offset=0,
        )
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert sent == ["accept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_unavailable", [False, True])
async def test_supervise_mirror_parks_then_releases_on_permission_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_unavailable: bool,
) -> None:
    created: list[object] = []
    finalized = asyncio.Event()
    second_started = asyncio.Event()
    monkeypatch.setattr(knp, "_POLL_INTERVAL_S", 0.01)

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            self.posts: list[tuple[str, dict]] = []
            created.append(self)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            assert cancelled.is_set() and finalized.is_set()
            self.posts.append((url, json))
            return httpx.Response(
                503 if receipt_unavailable else 200, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(knp.httpx, "AsyncClient", _FakeAsyncClient)

    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_run_one(_client: object, **_kw: object) -> None:
        if started.is_set():
            second_started.set()
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.sleep(0.01)
            finalized.set()
            raise

    monkeypatch.setattr(knp, "_run_one_permission", _fake_run_one)
    record_file = acp_record_path(tmp_path)
    record_file.write_bytes(b"")

    task = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_3",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.sleep(0.05)
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_msg("req-1")))
        await asyncio.wait_for(started.wait(), 2.0)
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_result_msg("req-1"), direction="in"))
        for _ in range(400):
            if created and getattr(created[0], "posts", None):
                break
            await asyncio.sleep(0.005)
        assert created
        url, body = created[0].posts[0]  # type: ignore[attr-defined]
        assert url == "/v1/sessions/conv_3/events"
        assert body["type"] == "external_elicitation_resolved"
        assert body["data"]["elicitation_id"] == kiro_permission_elicitation_id("conv_3", "req-1")
        await asyncio.wait_for(cancelled.wait(), 2.0)
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_msg("req-2")))
        await asyncio.wait_for(second_started.wait(), 2.0)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervise_mirror_skips_request_resolved_in_same_poll_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: list[object] = []

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            self.posts: list[tuple[str, dict]] = []
            created.append(self)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            self.posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(knp.httpx, "AsyncClient", _FakeAsyncClient)
    run_one_calls: list[str] = []

    async def _fake_run_one(_client: object, *, permission: object, **_kw: object) -> None:
        run_one_calls.append(permission.request_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(knp, "_run_one_permission", _fake_run_one)
    record_file = acp_record_path(tmp_path)
    record_file.write_bytes(b"")

    task = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_4",
            bridge_dir=tmp_path,
            poll_interval_s=0.02,
        )
    )
    try:
        await asyncio.sleep(0.08)
        with record_file.open("ab") as handle:
            handle.write(
                _record_bytes(_permission_msg("req-1"))
                + _record_bytes(_permission_result_msg("req-1"), direction="in")
            )
        await asyncio.sleep(0.2)
        assert run_one_calls == []
        assert created
        assert created[0].posts == []  # type: ignore[attr-defined]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervise_mirror_queues_requests_while_one_is_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(knp.httpx, "AsyncClient", _FakeAsyncClient)
    request_ids = ("req-1", "req-2", "req-3")
    started = {request_id: asyncio.Event() for request_id in request_ids}
    release = {request_id: asyncio.Event() for request_id in request_ids}
    run_one_calls: list[str] = []

    async def _fake_run_one(_client: object, *, permission: object, **_kw: object) -> None:
        request_id = permission.request_id  # type: ignore[attr-defined]
        run_one_calls.append(request_id)
        started[request_id].set()
        await release[request_id].wait()

    monkeypatch.setattr(knp, "_run_one_permission", _fake_run_one)
    record_file = acp_record_path(tmp_path)
    record_file.write_bytes(b"")

    task = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_5",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.sleep(0.05)
        with record_file.open("ab") as handle:
            handle.write(b"".join(_record_bytes(_permission_msg(rid)) for rid in request_ids))
        await asyncio.wait_for(started["req-1"].wait(), 2.0)
        assert run_one_calls == ["req-1"]

        release["req-1"].set()
        await asyncio.wait_for(started["req-2"].wait(), 2.0)
        assert run_one_calls == ["req-1", "req-2"]

        release["req-2"].set()
        await asyncio.wait_for(started["req-3"].wait(), 2.0)
        assert run_one_calls == ["req-1", "req-2", "req-3"]
    finally:
        for event in release.values():
            event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervise_mirror_drops_queued_request_resolved_in_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(knp.httpx, "AsyncClient", _FakeAsyncClient)
    started = asyncio.Event()
    release = asyncio.Event()
    run_one_calls: list[str] = []

    async def _fake_run_one(_client: object, *, permission: object, **_kw: object) -> None:
        request_id = permission.request_id  # type: ignore[attr-defined]
        run_one_calls.append(request_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(knp, "_run_one_permission", _fake_run_one)
    record_file = acp_record_path(tmp_path)
    record_file.write_bytes(b"")

    task = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_queued_terminal_resolution",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.sleep(0.05)
        with record_file.open("ab") as handle:
            handle.write(
                _record_bytes(_permission_msg("req-1")) + _record_bytes(_permission_msg("req-2"))
            )
        await asyncio.wait_for(started.wait(), 2.0)
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_result_msg("req-2"), direction="in"))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.05)
        assert run_one_calls == ["req-1"]
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_supervise_mirror_reaps_finished_task_and_mirrors_next_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A finished delivery task frees the slot so a later prompt is still mirrored.

    Without reaping done tasks, a completed (or failed) web-delivery would keep
    the single-prompt slot occupied forever and silently block every later
    prompt from the web mirror.
    """

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(knp.httpx, "AsyncClient", _FakeAsyncClient)
    run_one_calls: list[str] = []

    async def _fake_run_one(_client: object, *, permission: object, **_kw: object) -> None:
        # Returns immediately, so the parked task finishes without a recorder
        # response event ever arriving (mimics a delivered/failed verdict).
        run_one_calls.append(permission.request_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(knp, "_run_one_permission", _fake_run_one)
    record_file = acp_record_path(tmp_path)
    record_file.write_bytes(b"")

    task = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_6",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.sleep(0.05)
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_msg("req-1")))
        for _ in range(400):
            if run_one_calls == ["req-1"]:
                break
            await asyncio.sleep(0.005)
        assert run_one_calls == ["req-1"]
        # No response event for req-1 ever arrives; the reaper must still free
        # the slot once its task is done so req-2 gets mirrored.
        with record_file.open("ab") as handle:
            handle.write(_record_bytes(_permission_msg("req-2")))
        for _ in range(400):
            if run_one_calls == ["req-1", "req-2"]:
                break
            await asyncio.sleep(0.005)
        assert run_one_calls == ["req-1", "req-2"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [None, 429, 503, "connection"])
async def test_delivery_waits_for_explicit_retry_before_sending_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage: int | str | None,
) -> None:
    # Expected failures arrive without real backoff; skip costly traceback rendering.
    caplog.set_level("CRITICAL", logger=knp.__name__)
    monkeypatch.setattr(knp, "_DELIVERY_CONFIRM_TIMEOUT_S", 0)
    sleep = AsyncMock()
    monkeypatch.setattr(knp, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    asked_to_retry = asyncio.Event()
    user_retries = asyncio.Event()
    requests: list[dict] = []
    sent: list[str] = []
    failures = 10 if outage else 0

    async def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if request.url.path.endswith("/events"):
            return httpx.Response(202)
        delivery = data["native_delivery"]
        requests.append(data)
        if len(requests) > 1:
            assert delivery["state"] == "unconfirmed"
            assert delivery["action"] == "accept"
            assert sent == ["accept"]
            assert data == requests[1]
            if len(requests) <= failures + 1:
                if outage == "connection":
                    raise httpx.ConnectError("offline", request=request)
                return httpx.Response(outage)
            asked_to_retry.set()
            await user_retries.wait()
        return httpx.Response(
            200,
            json={"action": "accept", "native_delivery_attempt": delivery["attempt"]},
        )

    def send(bridge_dir: Path, *, action: str, **kwargs: object) -> None:
        sent.append(action)
        if len(sent) == 2:
            acp_record_path(bridge_dir).write_bytes(_record_bytes(_permission_result_msg()))

    monkeypatch.setattr(knp, "send_kiro_permission_verdict", send)
    permission = parse_permission_request(_permission_msg())
    assert permission is not None
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        task = asyncio.create_task(
            knp._run_one_permission(
                client,
                session_id="session",
                bridge_dir=tmp_path,
                permission=permission,
                elicitation_id="elicitation",
                record_offset=0,
            )
        )
        try:
            async with asyncio.timeout(budget(5)):
                await asked_to_retry.wait()
            assert sent == ["accept"]
            assert not task.done()
            delays = [call.args[0] for call in sleep.await_args_list]
            assert len(delays) == failures
            for delay, ceiling in zip(
                delays, [0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 30, 30, 30][:failures], strict=True
            ):
                assert ceiling / 2 <= delay <= ceiling
            user_retries.set()
            async with asyncio.timeout(budget(5)):
                await task
            assert sent == ["accept", "accept"]
            assert len(requests) == failures + 2
            assert (
                requests[0]["native_delivery"]["attempt"]
                != requests[1]["native_delivery"]["attempt"]
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["terminal_exited", "wrong_attempt", "missing_retry_ack"])
async def test_delivery_stops_on_terminal_exit_or_unacknowledged_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(knp, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.01)
    monkeypatch.setattr(knp, "_POLL_INTERVAL_S", 0.001)
    hooks = []
    sent = []

    async def handle(request: httpx.Request) -> httpx.Response:
        delivery = json.loads(request.content)["native_delivery"]
        hooks.append(delivery)
        result = {"action": "accept", "native_delivery_attempt": delivery["attempt"]}
        if failure == "wrong_attempt":
            result["native_delivery_attempt"] = "stale"
        elif failure == "missing_retry_ack" and len(hooks) > 1:
            del result["native_delivery_attempt"]
        assert len(hooks) <= 2
        return httpx.Response(200, json=result)

    def send(*args, **kwargs):
        sent.append(kwargs["action"])
        if failure == "terminal_exited":
            raise knp.KiroTerminalExited("kiro terminal is no longer running")

    monkeypatch.setattr(knp, "send_kiro_permission_verdict", send)
    permission = parse_permission_request(_permission_msg())
    assert permission is not None
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        async with asyncio.timeout(2):
            await knp._run_one_permission(
                client,
                session_id="session",
                bridge_dir=tmp_path,
                permission=permission,
                elicitation_id="eid",
                record_offset=0,
            )
    assert len(hooks) == (2 if failure == "missing_retry_ack" else 1)
    assert sent == ([] if failure == "wrong_attempt" else ["accept"])
