"""Unit tests for the qwen-native tool-approval mirror.

Covers everything a live ``qwen`` TUI isn't needed for, with the file + HTTP
boundaries faked:

* **Parser** — pulling a ``can_use_tool`` control request out of a ``--json-file``
  event (tool name / shell-command preview / request id), ignoring non-approval
  events, extracting a ``control_response`` request id, and the elicitation-id
  format.
* **Reader** — ``_read_new_control_events`` incremental byte-offset tailing,
  partial-line handling, and truncation rewind (the analog of the forwarder's
  ``_read_new_events``, but for the control plane).
* **Mirror supervisor** — ``_run_one_approval`` (park → verdict →
  ``confirmation_response``), ``_post_external_elicitation_resolved`` (un-park on
  a TUI-side answer), and ``supervise_qwen_approval_mirror`` (park a new request,
  then release it when its ``control_response`` arrives unanswered).

The event shapes are pinned to qwen's dual-output protocol (see
``docs/QWEN_NATIVE_DESIGN.md`` and qwen's ``dual-output.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from omnigent import qwen_native_permissions as qnp
from omnigent.qwen_native_bridge import events_file_path, input_file_path
from omnigent.qwen_native_permissions import (
    parse_can_use_tool,
    qwen_permission_elicitation_id,
)


def _can_use_tool_ev(
    request_id: str, *, tool_name: str = "run_shell_command", command: str = "ls"
) -> dict:
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "tool_use_id": f"tu_{request_id}",
            "input": {"command": command},
        },
    }


def _control_response_ev(request_id: str, *, allowed: bool = True) -> dict:
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": {"allowed": allowed},
        },
    }


def _ev_bytes(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


# ── Parser ───────────────────────────────────────────────────────


def test_parse_can_use_tool_extracts_request_id_name_and_command_preview() -> None:
    req = parse_can_use_tool(_can_use_tool_ev("r1", command="rm -rf build"))
    assert req is not None
    assert req.request_id == "r1"
    assert req.tool_name == "run_shell_command"
    assert req.preview == "rm -rf build"
    assert req.message


def test_parse_can_use_tool_non_shell_input_falls_back_to_json_preview() -> None:
    ev = {
        "type": "control_request",
        "request_id": "r2",
        "request": {"subtype": "can_use_tool", "tool_name": "write_file", "input": {"path": "/x"}},
    }
    req = parse_can_use_tool(ev)
    assert req is not None
    assert req.tool_name == "write_file"
    assert json.loads(req.preview) == {"path": "/x"}


def test_parse_can_use_tool_missing_name_degrades_to_tool() -> None:
    ev = {"type": "control_request", "request_id": "r3", "request": {"subtype": "can_use_tool"}}
    req = parse_can_use_tool(ev)
    assert req is not None
    assert req.tool_name == "tool"
    assert req.preview == "tool"


@pytest.mark.parametrize(
    "ev",
    [
        pytest.param({"type": "assistant", "uuid": "a1"}, id="assistant"),
        pytest.param({"type": "system", "subtype": "session_start"}, id="system"),
        pytest.param(
            {"type": "control_request", "request": {"subtype": "other"}, "request_id": "x"},
            id="other-control-subtype",
        ),
        pytest.param(
            {"type": "control_request", "request": {"subtype": "can_use_tool"}},
            id="missing-request-id",
        ),
    ],
)
def test_parse_can_use_tool_returns_none_for_non_approval_events(ev: dict) -> None:
    assert parse_can_use_tool(ev) is None


def test_control_response_request_id_extraction() -> None:
    assert qnp._control_response_request_id(_control_response_ev("r9")) == "r9"
    assert qnp._control_response_request_id({"type": "control_request"}) is None
    assert qnp._control_response_request_id({"type": "control_response", "response": {}}) is None


def test_elicitation_id_is_deterministic_and_session_scoped() -> None:
    eid = qwen_permission_elicitation_id("conv_abc", "r1")
    assert eid == qwen_permission_elicitation_id("conv_abc", "r1")
    assert eid != qwen_permission_elicitation_id("conv_xyz", "r1")
    assert "conv_abc" in eid
    assert eid.startswith("elicit_qwen_")


# ── Reader ───────────────────────────────────────────────────────


def test_read_new_control_events_incremental_and_partial_line(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    f.write_bytes(_ev_bytes(_can_use_tool_ev("r1")))
    events, off = qnp._read_new_control_events(f, 0)
    assert [e.kind for e in events] == ["request"]
    assert events[0].request_id == "r1"

    # Append a complete response line plus a trailing partial line; only the
    # complete one is consumed, offset stops before the partial.
    with open(f, "ab") as fh:
        fh.write(_ev_bytes(_control_response_ev("r1")))
        fh.write(b'{"type":"control_request","request_id":"r2"')  # no newline
    events2, off2 = qnp._read_new_control_events(f, off)
    assert [(e.kind, e.request_id) for e in events2] == [("response", "r1")]
    assert off2 == off + len(_ev_bytes(_control_response_ev("r1")))


def test_read_new_control_events_rewinds_on_truncation(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    f.write_bytes(_ev_bytes(_can_use_tool_ev("r1")) * 3)
    _events, off = qnp._read_new_control_events(f, 0)
    assert off > 0
    # Terminal relaunch truncates the file; a stale offset past EOF rewinds to 0.
    f.write_bytes(_ev_bytes(_can_use_tool_ev("r2")))
    events, new_off = qnp._read_new_control_events(f, off)
    assert [e.request_id for e in events] == ["r2"]
    assert new_off == len(_ev_bytes(_can_use_tool_ev("r2")))


def test_read_new_control_events_ignores_transcript_and_malformed(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    f.write_bytes(
        _ev_bytes({"type": "assistant", "uuid": "a1", "message": {"content": []}})
        + b"not-json\n"
        + _ev_bytes(_can_use_tool_ev("r1"))
    )
    events, _off = qnp._read_new_control_events(f, 0)
    assert [(e.kind, e.request_id) for e in events] == [("request", "r1")]


# ── Mirror supervisor ────────────────────────────────────────────


class _QueueClient:
    """Async httpx-client stub: records POSTs, returns queued responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
        self.posts.append((url, json))
        return self._responses.pop(0)


@pytest.mark.parametrize(
    ("response", "expected_allowed"),
    [
        pytest.param(httpx.Response(200, json={"action": "accept"}), True, id="accept->allow"),
        pytest.param(httpx.Response(200, json={"action": "decline"}), False, id="decline->deny"),
        pytest.param(httpx.Response(200, json={"action": "cancel"}), False, id="cancel->deny"),
        pytest.param(httpx.Response(200), None, id="empty-200->no-write"),
        pytest.param(httpx.Response(400, text="nope"), None, id="rejected->no-write"),
        pytest.param(httpx.Response(200, content=b"not-json"), None, id="non-json->no-write"),
        pytest.param(
            httpx.Response(200, json={"action": "??"}), None, id="unknown-action->no-write"
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_one_approval_posts_then_writes_confirmation(
    response: httpx.Response,
    expected_allowed: bool | None,
    tmp_path: Path,
) -> None:
    """Park a request on the server, then answer qwen with a confirmation line.

    A ``confirmation_response`` is written ONLY for a concrete accept/decline/
    cancel verdict; an empty 2xx (TUI answered / timeout), a rejection, a
    non-JSON body, or an unknown action writes nothing to the input file.
    """
    req = parse_can_use_tool(_can_use_tool_ev("r1", command="ls -la"))
    assert req is not None
    client = _QueueClient([response])

    await qnp._run_one_approval(
        client,  # type: ignore[arg-type]
        session_id="conv_1",
        bridge_dir=tmp_path,
        approval=req,
        elicitation_id="elic_1",
    )

    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/hooks/native-permission-request"
    assert body == {
        "elicitation_id": "elic_1",
        "agent": "qwen",
        "policy_name": "qwen_native_permission",
        "operation_type": "run_shell_command",
        "message": req.message,
        "content_preview": "ls -la",
    }

    in_path = input_file_path(tmp_path)
    if expected_allowed is None:
        assert not in_path.exists() or in_path.read_text() == ""
    else:
        lines = [json.loads(ln) for ln in in_path.read_text().splitlines() if ln.strip()]
        assert lines == [
            {"type": "confirmation_response", "request_id": "r1", "allowed": expected_allowed}
        ]


@pytest.mark.asyncio
async def test_post_external_elicitation_resolved_shape() -> None:
    client = _QueueClient([httpx.Response(200)])
    await qnp._post_external_elicitation_resolved(client, "conv_2", "elic_9")  # type: ignore[arg-type]
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_2/events"
    assert body == {"type": "external_elicitation_resolved", "data": {"elicitation_id": "elic_9"}}


@pytest.mark.asyncio
async def test_supervise_mirror_parks_then_releases_on_control_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Park a new ``can_use_tool``, then release it when its ``control_response``
    arrives while the web card is still parked (the user answered in the TUI).
    """
    created: list[object] = []

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            self.posts: list[tuple[str, dict]] = []
            created.append(self)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            self.posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(qnp.httpx, "AsyncClient", _FakeAsyncClient)

    # Hold the park task pending so the release branch is taken (a completed task
    # means the web verdict already landed → no release needed).
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run_one(_client: object, **_kw: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(qnp, "_run_one_approval", _fake_run_one)

    events_file = events_file_path(tmp_path)
    events_file.write_bytes(b"")  # mirror seeds offset at EOF (==0 here)

    task = asyncio.create_task(
        qnp.supervise_qwen_approval_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_3",
            bridge_dir=tmp_path,
            poll_interval_s=0.001,
        )
    )
    try:
        # Let the mirror seed its read offset at EOF (empty file) first, so the
        # request below is seen as NEW — the real fresh-terminal flow.
        await asyncio.sleep(0.05)
        # First a control_request appears → park task spawns.
        with open(events_file, "ab") as fh:
            fh.write(_ev_bytes(_can_use_tool_ev("r1")))
        await asyncio.wait_for(started.wait(), 2.0)
        # Then its control_response (answered in the TUI) → release the card.
        with open(events_file, "ab") as fh:
            fh.write(_ev_bytes(_control_response_ev("r1")))
        for _ in range(400):
            if created and getattr(created[0], "posts", None):
                break
            await asyncio.sleep(0.005)
        assert created, "supervisor never opened a client"
        url, body = created[0].posts[0]  # type: ignore[attr-defined]
        assert url == "/v1/sessions/conv_3/events"
        assert body["type"] == "external_elicitation_resolved"
        assert body["data"]["elicitation_id"] == qwen_permission_elicitation_id("conv_3", "r1")
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
    """A request + its control_response in ONE poll batch never parks a card.

    The decision was made (TUI/auto) within a single poll window, before the
    mirror could park it. Parking now would race its own response — the response
    branch would run against a freshly-created task that hasn't POSTed yet and so
    couldn't release the card, leaving it stuck until the server-side timeout. So
    the mirror must spawn no approval task and post nothing for that request.
    """
    created: list[object] = []

    class _FakeAsyncClient:
        def __init__(self, **_kw: object) -> None:
            self.posts: list[tuple[str, dict]] = []
            created.append(self)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def post(self, url: str, *, json: dict, **_kw: object) -> httpx.Response:
            self.posts.append((url, json))
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(qnp.httpx, "AsyncClient", _FakeAsyncClient)

    run_one_calls: list[str] = []

    async def _fake_run_one(_client: object, *, approval: object, **_kw: object) -> None:
        run_one_calls.append(approval.request_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(qnp, "_run_one_approval", _fake_run_one)

    events_file = events_file_path(tmp_path)
    events_file.write_bytes(b"")  # mirror seeds offset at EOF (==0 here)

    task = asyncio.create_task(
        qnp.supervise_qwen_approval_mirror(
            base_url="http://t",
            headers={},
            session_id="conv_4",
            bridge_dir=tmp_path,
            poll_interval_s=0.02,
        )
    )
    try:
        # Let the mirror seed its offset at EOF and enter its sleep, then write
        # BOTH events in a single atomic write so the next poll reads them as one
        # batch (the file is empty until this one syscall completes, so a poll
        # can only see neither line or both — never just the request).
        await asyncio.sleep(0.08)
        with open(events_file, "ab") as fh:
            fh.write(_ev_bytes(_can_use_tool_ev("r1")) + _ev_bytes(_control_response_ev("r1")))
        # Give the supervisor several poll cycles to consume the batch.
        await asyncio.sleep(0.2)
        assert run_one_calls == [], "should not park a request already resolved in-batch"
        assert created, "supervisor never opened a client"
        assert created[0].posts == [], "should not post external_elicitation_resolved"  # type: ignore[attr-defined]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
