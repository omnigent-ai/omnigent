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

from pathlib import Path

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_file_tool

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
