"""Unit tests for the runner-side ``upload_file`` dispatch containment.

These cover the upload-containment fix: the runner's ``_execute_file_tool``
upload branch must resolve the agent-supplied ``path`` against the
session workspace and reject anything that escapes it, instead of
calling ``open(path, "rb")`` on the raw argument (which let an agent
read arbitrary host files from the un-sandboxed runner process).

The dispatch posts uploaded bytes to the session file store over
HTTP, so we drive it with an ``httpx.MockTransport`` that records
every request. A rejected path must produce an error string AND make
no POST at all (nothing is read or exfiltrated); an in-workspace path
must POST the file's real bytes and return the store's ``file_id``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_file_tool, execute_tool

_CONVERSATION_ID = "conv_sec20870"


def _recording_client(captured: list[httpx.Request]) -> httpx.AsyncClient:
    """
    Build an AP-server client that records requests and returns a
    created-file response.

    :param captured: List the handler appends each inbound request to,
        so the test can assert whether (and what) the dispatch POSTed.
    :returns: An ``httpx.AsyncClient`` backed by a mock transport that
        answers the files endpoint with ``201`` and a fixed file id.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"id": "file_abc123"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://ap-server",
    )


@pytest.mark.parametrize(
    "evil_path",
    [
        "/etc/passwd",  # absolute escape
        "../../../etc/passwd",  # parent traversal
        "../secrets.txt",  # single-level traversal just outside root
    ],
)
@pytest.mark.asyncio
async def test_upload_rejects_paths_outside_workspace(
    evil_path: str,
    tmp_path: Path,
) -> None:
    """
    An ``upload_file`` path that escapes the workspace is rejected and
    no upload POST is made.

    :param evil_path: An agent-supplied path that resolves outside the
        workspace root, e.g. ``"/etc/passwd"``.
    :param tmp_path: Pytest-provided workspace root for the session.
    """
    # A real, readable host file outside the workspace would be the
    # exfiltration target. Create one next to the workspace so the
    # traversal cases point at a file that actually exists — the
    # rejection must happen on path containment, NOT on the file
    # being absent.
    outside = tmp_path.parent / "secrets.txt"
    outside.write_text("TOP SECRET")

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await _execute_file_tool(
            "upload_file",
            {"path": evil_path},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            runner_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    # Rejection surfaces as an error string from safe_resolve's
    # ValueError. If this were a success JSON ({"file_id": ...}) the
    # containment check failed open and the file was exfiltrated.
    assert result.startswith("Error: sys_upload_file failed:"), (
        f"Expected a containment rejection for {evil_path!r}, got: {result!r}"
    )
    assert "escapes" in result, (
        f"Expected the workspace-escape reason in the error for {evil_path!r}, got: {result!r}"
    )
    # The decisive security assertion: nothing was POSTed, so the host
    # file was never read into the session store. A non-empty list here
    # means the runner read and uploaded an out-of-workspace file.
    assert captured == [], (
        f"upload_file POSTed despite an out-of-workspace path {evil_path!r}; "
        f"the host file would have been exfiltrated"
    )


@pytest.mark.asyncio
async def test_upload_rejects_symlink_escaping_workspace(tmp_path: Path) -> None:
    """
    A workspace-local symlink that points at a host file outside the
    workspace is rejected, and no upload POST is made.

    This is the symlink variant of the containment check: an agent
    could plant a relative symlink inside its workspace whose target
    is a host secret. The dispatch must follow the link, see the
    target escapes the root, and refuse — never reading or uploading
    the linked file.

    :param tmp_path: Pytest-provided workspace root for the session.
    """
    secret = tmp_path.parent / "host_secret.txt"
    secret.write_text("root:x:0:0:exfiltrated")
    link = tmp_path / "looks_innocent.txt"
    link.symlink_to(secret)

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await _execute_file_tool(
            "upload_file",
            {"path": "looks_innocent.txt"},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            runner_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    # The resolved symlink target escapes the workspace, so the upload
    # is rejected before any read. A success envelope here would mean
    # the host secret was followed and exfiltrated via the symlink.
    assert result.startswith("Error: sys_upload_file failed:"), (
        f"Expected a containment rejection for the escaping symlink, got: {result!r}"
    )
    assert "escapes" in result, f"Expected a workspace-escape reason, got: {result!r}"
    # Decisive: nothing POSTed, so the symlinked host file was never read.
    assert captured == [], (
        "upload_file POSTed despite a symlink whose target escapes the "
        "workspace; the host secret would have been exfiltrated"
    )


@pytest.mark.asyncio
async def test_upload_in_workspace_succeeds(tmp_path: Path) -> None:
    """
    An ``upload_file`` path inside the workspace uploads the file's raw
    bytes and returns the store-issued ``file_id``.

    :param tmp_path: Pytest-provided workspace root for the session.
    """
    # Binary payload with a NUL byte proves the dispatch uploads raw
    # bytes (not a text/line-converted view): if the read path ever
    # routed through a text reader the NUL-containing content would be
    # corrupted or rejected.
    payload = b"chart-bytes\x00\x01\x02 end"
    target = tmp_path / "output" / "chart.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await _execute_file_tool(
            "upload_file",
            {"path": "output/chart.png"},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            runner_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    # Success returns the file store's id and the resolved basename —
    # proving the relative in-workspace path was accepted and uploaded.
    assert result == '{"file_id": "file_abc123", "filename": "chart.png"}', (
        f"Expected a success envelope with the store file id, got: {result!r}"
    )
    # Exactly one POST, to the session's files endpoint.
    assert len(captured) == 1, f"Expected exactly one upload POST, got {len(captured)} request(s)"
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == f"/v1/sessions/{_CONVERSATION_ID}/resources/files"
    # The multipart body must carry the real file bytes (incl. the NUL
    # byte) — confirming the actual workspace file content traversed the
    # pipeline, not an empty or placeholder upload.
    assert payload in request.content, (
        "Uploaded multipart body did not contain the workspace file's raw bytes"
    )


# ── sys_session_send file_ids forwarding (parent → child at spawn) ──
#
# When ``sys_session_send`` carries ``file_ids``, the runner copies those
# parent files into the freshly-created child via the lineage-scoped copy
# endpoint, then attaches one file block per copied id to the child's
# first-turn content. These drive the dispatch end-to-end with a recording
# server transport, mirroring the upload-containment tests above and the
# ``sys_session_send`` tests in ``test_runner_dispatch.py``.

_PARENT_ID = "conv_parent_files"
_CHILD_ID = "conv_child_files"


def _spec_with_subagent() -> SimpleNamespace:
    """A parent-spec stub declaring one ``worker`` sub-agent (no harness CLI)."""
    return SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")])


def _spawn_server_handler(
    *,
    events: list[dict[str, Any]],
    copies: list[dict[str, Any]],
    mapping: dict[str, str],
    file_meta: dict[str, dict[str, Any]],
    copy_status: int = 200,
    copy_error: dict[str, Any] | None = None,
    deletes: list[str] | None = None,
):
    """
    Build a mock Omnigent-server handler for a fresh named spawn.

    Serves the no-existing-child lookup, the child create, the copy
    endpoint (recording its body and answering with ``mapping`` or an
    error), per-file metadata GETs, and the child events POST (recording
    its body).

    :param events: List the handler appends each child events body to.
    :param copies: List the handler appends each copy request body to.
    :param mapping: ``{old_id: new_id}`` returned by the copy endpoint.
    :param file_meta: ``{new_id: resource_dict}`` for the metadata GETs.
    :param copy_status: HTTP status the copy endpoint returns.
    :param copy_error: Error body for a non-2xx copy response.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_ID}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "/v1/sessions":
            return httpx.Response(201, json={"id": _CHILD_ID})
        if request.method == "POST" and path == f"/v1/sessions/{_CHILD_ID}/resources/files:copy":
            copies.append(json.loads(request.content))
            if copy_status >= 400:
                return httpx.Response(copy_status, json=copy_error or {"error": {}})
            return httpx.Response(
                copy_status,
                json={
                    "object": "session.files.copied",
                    "session_id": _CHILD_ID,
                    "mapping": mapping,
                },
            )
        if request.method == "GET" and path.startswith(
            f"/v1/sessions/{_CHILD_ID}/resources/files/"
        ):
            new_id = path.rsplit("/", 1)[-1]
            meta = file_meta.get(new_id)
            if meta is None:
                return httpx.Response(404, json={"error": {"message": "missing"}})
            return httpx.Response(200, json=meta)
        if request.method == "POST" and path == f"/v1/sessions/{_CHILD_ID}/events":
            events.append(json.loads(request.content))
            return httpx.Response(202, json={"queued": True})
        if request.method == "DELETE" and path == f"/v1/sessions/{_CHILD_ID}":
            if deletes is not None:
                deletes.append(_CHILD_ID)
            return httpx.Response(200, json={"id": _CHILD_ID, "deleted": True})
        return httpx.Response(404, json={"error": str(request.url)})

    return _handler


async def _run_spawn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    args_payload: Any,
    handler,
) -> str:
    """Dispatch one ``sys_session_send`` against ``handler`` and clean up."""
    from omnigent.runner import app as runner_app

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        try:
            return await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "title": "task-1", "args": args_payload}),
                server_client=server_client,
                conversation_id=_PARENT_ID,
                agent_spec=_spec_with_subagent(),
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_child_session(_CHILD_ID)
            runner_app.unregister_subagent_work(_CHILD_ID)
            runner_app._session_inboxes_ref.pop(_PARENT_ID, None)


def _file_resource(new_id: str, filename: str) -> dict[str, Any]:
    """A ``session.resource`` (type file) metadata dict for ``new_id``."""
    return {
        "id": new_id,
        "object": "session.resource",
        "type": "file",
        "session_id": _CHILD_ID,
        "name": filename,
        "metadata": {"filename": filename, "bytes": 10, "created_at": 0},
    }


@pytest.mark.asyncio
async def test_send_with_file_ids_copies_then_attaches_input_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One ``file_ids`` entry copies parent→child, then the child events
    content carries input_text + one input_file block on the MAPPED id.

    Asserts the copy endpoint receives ``(parent_session, file_ids)`` and
    that the posted block references the new child-scoped id, not the
    original parent id — proving the runner threads the mapping through.
    """
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_parent": "file_child"},
        file_meta={"file_child": _file_resource("file_child", "notes.txt")},
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "use this", "file_ids": ["file_parent"]},
        handler=handler,
    )

    assert json.loads(output)["status"] == "launching"
    # Exactly one copy, addressed parent→child with the requested ids.
    assert len(copies) == 1
    assert copies[0] == {"source_session_id": _PARENT_ID, "file_ids": ["file_parent"]}
    # The child message: input_text first, then the file block on the
    # mapped id (NOT the original parent id).
    content = events[0]["data"]["content"]
    assert content[0] == {"type": "input_text", "text": "use this"}
    assert content[1] == {"type": "input_file", "file_id": "file_child"}


@pytest.mark.asyncio
async def test_send_with_image_file_id_attaches_input_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``image/*`` file (by filename) yields an ``input_image`` block."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_pic": "file_child_pic"},
        file_meta={"file_child_pic": _file_resource("file_child_pic", "chart.png")},
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "look", "file_ids": ["file_pic"]},
        handler=handler,
    )

    assert json.loads(output)["status"] == "launching"
    content = events[0]["data"]["content"]
    assert content[1] == {"type": "input_image", "file_id": "file_child_pic"}


@pytest.mark.asyncio
async def test_send_multiple_file_ids_preserve_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple file_ids produce one block each, in request order, mapped."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"f_a": "c_a", "f_b": "c_b", "f_c": "c_c"},
        file_meta={
            "c_a": _file_resource("c_a", "a.pdf"),
            "c_b": _file_resource("c_b", "b.png"),
            "c_c": _file_resource("c_c", "c.csv"),
        },
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "three", "file_ids": ["f_a", "f_b", "f_c"]},
        handler=handler,
    )

    assert json.loads(output)["status"] == "launching"
    content = events[0]["data"]["content"]
    assert content == [
        {"type": "input_text", "text": "three"},
        {"type": "input_file", "file_id": "c_a"},
        {"type": "input_image", "file_id": "c_b"},
        {"type": "input_file", "file_id": "c_c"},
    ]


@pytest.mark.asyncio
async def test_send_without_file_ids_is_unchanged_and_skips_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A plain-string send (no file_ids) posts a single input_text block and
    never calls the copy endpoint — the text-only path is unchanged.
    """
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={},
        file_meta={},
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload="just text",
        handler=handler,
    )

    assert json.loads(output)["status"] == "launching"
    assert copies == [], "text-only send must not call the copy endpoint"
    assert events[0]["data"]["content"] == [{"type": "input_text", "text": "just text"}]


@pytest.mark.asyncio
async def test_send_with_bad_file_id_surfaces_copy_error_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A copy that 404s (e.g. a hallucinated file id) surfaces an error to
    the parent and posts no child event — never a malformed message.
    """
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    deletes: list[str] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={},
        file_meta={},
        copy_status=404,
        copy_error={"error": {"message": "File 'file_bogus' not found in source session"}},
        deletes=deletes,
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "use this", "file_ids": ["file_bogus"]},
        handler=handler,
    )

    assert output.startswith("Error: failed to copy files to child:"), output
    assert "404" in output
    # Decisive: the copy was attempted but no (malformed) child event was posted.
    assert len(copies) == 1
    assert events == [], "no child event may be posted when the file copy fails"
    # The freshly-created server child is torn down so it can't poison a
    # retry with the same (agent, title) as a phantom existing child.
    assert deletes == [_CHILD_ID], "failed spawn must delete the empty child session"
