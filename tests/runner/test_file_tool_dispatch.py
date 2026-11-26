"""Unit tests for the runner-side ``sys_files_upload`` dispatch containment.

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
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import resource_registry, subagent_work
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import (
    UNKNOWN_SESSION_WORKSPACE,
    UnknownSessionWorkspace,
    _execute_file_tool,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, ToolGroupsConfig

# The session file tools are off by default; these tests exercise them, so opt in.
_FILE_TOOLS_ON = ToolGroupsConfig(files=True)

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
    A ``sys_files_upload`` path that escapes the workspace is rejected and
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
            "sys_files_upload",
            {"path": evil_path},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            session_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    # Rejection surfaces as an error string from safe_resolve's
    # ValueError. If this were a success JSON ({"file_id": ...}) the
    # containment check failed open and the file was exfiltrated.
    assert result.startswith("Error: sys_files_upload failed:"), (
        f"Expected a containment rejection for {evil_path!r}, got: {result!r}"
    )
    assert "escapes" in result, (
        f"Expected the workspace-escape reason in the error for {evil_path!r}, got: {result!r}"
    )
    # The decisive security assertion: nothing was POSTed, so the host
    # file was never read into the session store. A non-empty list here
    # means the runner read and uploaded an out-of-workspace file.
    assert captured == [], (
        f"sys_files_upload POSTed despite an out-of-workspace path {evil_path!r}; "
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
            "sys_files_upload",
            {"path": "looks_innocent.txt"},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            session_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    # The resolved symlink target escapes the workspace, so the upload
    # is rejected before any read. A success envelope here would mean
    # the host secret was followed and exfiltrated via the symlink.
    assert result.startswith("Error: sys_files_upload failed:"), (
        f"Expected a containment rejection for the escaping symlink, got: {result!r}"
    )
    assert "escapes" in result, f"Expected a workspace-escape reason, got: {result!r}"
    # Decisive: nothing POSTed, so the symlinked host file was never read.
    assert captured == [], (
        "sys_files_upload POSTed despite a symlink whose target escapes the "
        "workspace; the host secret would have been exfiltrated"
    )


@pytest.mark.asyncio
async def test_upload_in_workspace_succeeds(tmp_path: Path) -> None:
    """
    A ``sys_files_upload`` path inside the workspace uploads the file's raw
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
            "sys_files_upload",
            {"path": "output/chart.png"},
            client,
            conversation_id=_CONVERSATION_ID,
            agent_spec=None,
            session_workspace=tmp_path,
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


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
@pytest.mark.asyncio
async def test_native_relay_upload_keeps_workspace_containment(
    harness: str,
    tmp_path: Path,
) -> None:
    """
    A native harness's relayed ``sys_files_upload`` is granted to a spec that
    turns the file tools on, yet a path outside the session workspace is still
    rejected unread.

    The relay reaches ``execute_tool`` with the session's spec, harness, runtime
    cwd and own workspace, as the runner's ``/mcp/execute`` route passes them.

    :param harness: Native harness the session runs, e.g. ``"claude-native"``.
    :param tmp_path: Pytest-provided workspace root for the session.
    """
    (tmp_path.parent / "secrets.txt").write_text("TOP SECRET")
    spec = AgentSpec(
        spec_version=1,
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": harness}),
        tool_groups=_FILE_TOOLS_ON,
    )

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await execute_tool(
            tool_name="sys_files_upload",
            arguments=json.dumps({"path": "../secrets.txt"}),
            server_client=client,
            agent_spec=spec,
            conversation_id=_CONVERSATION_ID,
            runner_workspace=tmp_path,
            session_workspace=tmp_path,
            effective_harness=harness,
        )
    finally:
        await client.aclose()

    # The containment error (not a grant refusal) proves the opted-in surface
    # admitted the call and the workspace check still ran.
    assert result.startswith("Error: sys_files_upload failed:"), result
    assert "escapes" in result, result
    assert captured == [], "a relayed upload read and posted a file outside the workspace"


def _placeholder_cwd_spec() -> AgentSpec:
    """A spec whose ``os_env`` leaves the workspace to the runner, like ``cwd: .``."""
    return AgentSpec(
        spec_version=1,
        skills_filter="none",
        os_env=OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none")),
        tool_groups=_FILE_TOOLS_ON,
    )


async def _upload(
    client: httpx.AsyncClient,
    path: str,
    *,
    agent_spec: AgentSpec,
    resource_registry: SessionResourceRegistry | None,
    runner_workspace: Path | None = None,
    session_workspace: Path | UnknownSessionWorkspace | None = None,
) -> str:
    """Call ``sys_files_upload`` the way the runner's ``/mcp/execute`` route does."""
    return await execute_tool(
        tool_name="sys_files_upload",
        arguments=json.dumps({"path": path}),
        server_client=client,
        resource_registry=resource_registry,
        agent_spec=agent_spec,
        conversation_id=_CONVERSATION_ID,
        runner_workspace=runner_workspace,
        session_workspace=session_workspace,
    )


def _default_temp_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Point the runner's default temp roots at a fresh directory, with no override.

    The agent's tools default under ``tempfile.gettempdir()`` and the session's
    filesystem routes under the registry's workspace parent; both move here.

    :param monkeypatch: Patches the two defaults and unsets the root override.
    :param tmp_path: Pytest-provided parent for the controlled root.
    :returns: The controlled temp root.
    """
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.delenv("OMNIGENT_RUNNER_OS_ENV_ROOT", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    monkeypatch.setattr(resource_registry, "_DEFAULT_WORKSPACE_PARENT", str(temp_root))
    return temp_root


@pytest.mark.asyncio
async def test_upload_without_any_workspace_reads_both_default_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    With no session or runner workspace, uploads read what the agent's tools
    wrote in their directory and what the session's filesystem routes hold in
    theirs: two separate per-session directories under the default temp roots.

    :param monkeypatch: Controls the default temp roots.
    :param tmp_path: Pytest-provided parent for the controlled roots.
    """
    temp_root = _default_temp_root(monkeypatch, tmp_path)
    registry = SessionResourceRegistry()
    spec = _placeholder_cwd_spec()
    written = await execute_tool(
        tool_name="sys_os_write",
        arguments=json.dumps({"path": "out/report.md", "content": "agent report"}),
        agent_spec=spec,
        conversation_id=_CONVERSATION_ID,
    )
    tools_dir = temp_root / "omnigent-runner-os-envs" / _CONVERSATION_ID / "workspace"
    session_dir = temp_root / "omnigent-sessions" / _CONVERSATION_ID / "workspace"
    assert registry.compute_default_env_root(_CONVERSATION_ID, spec) == str(session_dir.resolve())
    session_dir.mkdir(parents=True)
    (session_dir / "notes.txt").write_text("user notes")

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        agent_file = await _upload(
            client, "out/report.md", agent_spec=spec, resource_registry=registry
        )
        user_file = await _upload(client, "notes.txt", agent_spec=spec, resource_registry=registry)
    finally:
        await client.aclose()

    assert json.loads(written)["created"] is True
    assert (tools_dir / "out" / "report.md").read_text() == "agent report"
    assert json.loads(agent_file)["filename"] == "report.md", agent_file
    assert json.loads(user_file)["filename"] == "notes.txt", user_file
    assert len(captured) == 2
    assert b"agent report" in captured[0].content
    assert b"user notes" in captured[1].content


@pytest.mark.asyncio
async def test_upload_leaves_existing_no_workspace_directories_in_place(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A session from before this runner, with files in both default directories,
    keeps them all: its tools still read their directory, uploads reach both,
    the tools' copy of a name in both wins, and nothing is moved or rewritten.

    :param monkeypatch: Controls the default temp roots.
    :param tmp_path: Pytest-provided parent for the controlled roots.
    """
    temp_root = _default_temp_root(monkeypatch, tmp_path)
    tools_dir = temp_root / "omnigent-runner-os-envs" / _CONVERSATION_ID / "workspace"
    session_dir = temp_root / "omnigent-sessions" / _CONVERSATION_ID / "workspace"
    before = {
        tools_dir / "old.txt": "written before the upgrade",
        tools_dir / "shared.txt": "tools copy",
        session_dir / "notes.txt": "panel note",
        session_dir / "shared.txt": "panel copy",
    }
    for file, content in before.items():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    registry = SessionResourceRegistry()
    spec = _placeholder_cwd_spec()

    read = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "old.txt"}),
        agent_spec=spec,
        conversation_id=_CONVERSATION_ID,
    )
    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        uploads = [
            await _upload(client, name, agent_spec=spec, resource_registry=registry)
            for name in ("old.txt", "notes.txt", "shared.txt")
        ]
    finally:
        await client.aclose()

    assert "written before the upgrade" in read, read
    assert [json.loads(result)["filename"] for result in uploads] == [
        "old.txt",
        "notes.txt",
        "shared.txt",
    ]
    assert len(captured) == 3
    assert b"written before the upgrade" in captured[0].content
    assert b"panel note" in captured[1].content
    assert b"tools copy" in captured[2].content and b"panel copy" not in captured[2].content
    assert {file: file.read_text() for file in before} == before
    assert sorted(p.name for p in tools_dir.iterdir()) == ["old.txt", "shared.txt"]
    assert sorted(p.name for p in session_dir.iterdir()) == ["notes.txt", "shared.txt"]


@pytest.mark.parametrize("path_form", ["inside-runner-root", "parent-traversal", "absolute"])
@pytest.mark.asyncio
async def test_upload_on_an_isolating_runner_refuses_a_sibling_session_file(
    path_form: str,
    tmp_path: Path,
) -> None:
    """
    On a runner that gives each session its own subdirectory, a session without
    its own workspace uploads from that subdirectory, never a sibling's file.

    ``runner_workspace`` is the shared root, as the runtime cwd falls back to it.

    :param path_form: How the agent names the sibling's file.
    :param tmp_path: Pytest-provided parent of the runner workspace.
    """
    runner_root = tmp_path / "runner"
    registry = SessionResourceRegistry(runner_workspace=runner_root, per_session_workspace=True)
    spec = _placeholder_cwd_spec()
    own_root = registry.compute_default_env_root(_CONVERSATION_ID, spec)
    sibling_root = registry.compute_default_env_root("conv_sibling", spec)
    assert own_root is not None and sibling_root is not None
    Path(own_root).mkdir(parents=True)
    Path(own_root, "own.txt").write_text("mine")
    Path(sibling_root).mkdir(parents=True)
    secret = Path(sibling_root, "secret.txt")
    secret.write_text("SIBLING SECRET")
    sibling_path = {
        # Relative to the shared runner root, which holds the sibling's directory.
        "inside-runner-root": f"{secret.parent.name}/secret.txt",
        "parent-traversal": f"../{secret.parent.name}/secret.txt",
        "absolute": str(secret),
    }[path_form]

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        refused = await _upload(
            client,
            sibling_path,
            agent_spec=spec,
            resource_registry=registry,
            runner_workspace=runner_root,
        )
        posted_before_own_upload = list(captured)
        own = await _upload(
            client,
            "own.txt",
            agent_spec=spec,
            resource_registry=registry,
            runner_workspace=runner_root,
        )
    finally:
        await client.aclose()

    assert refused.startswith("Error: sys_files_upload failed:"), refused
    assert posted_before_own_upload == [], "the sibling session's file was uploaded"
    assert json.loads(own)["filename"] == "own.txt", own
    assert len(captured) == 1
    assert b"mine" in captured[0].content


@pytest.mark.asyncio
async def test_upload_reads_the_explicit_session_workspace_on_an_isolating_runner(
    tmp_path: Path,
) -> None:
    """
    A session's own workspace stays its boundary on a runner that isolates
    sessions, as for the CLI's native wrappers, which use the launch directory.

    :param tmp_path: Pytest-provided parent of the launch directory.
    """
    launch_dir = tmp_path / "project"
    launch_dir.mkdir()
    (launch_dir / "report.txt").write_text("project report")
    registry = SessionResourceRegistry(runner_workspace=launch_dir, per_session_workspace=True)
    spec = AgentSpec(
        spec_version=1,
        skills_filter="none",
        executor=ExecutorSpec(config={"harness": "claude-native"}),
        tool_groups=_FILE_TOOLS_ON,
    )

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await _upload(
            client,
            "report.txt",
            agent_spec=spec,
            resource_registry=registry,
            runner_workspace=launch_dir,
            session_workspace=launch_dir,
        )
    finally:
        await client.aclose()

    assert json.loads(result)["filename"] == "report.txt", result
    assert len(captured) == 1
    assert b"project report" in captured[0].content


@pytest.mark.parametrize("unreadable", [False, True], ids=["no-registry", "unreadable-session"])
@pytest.mark.asyncio
async def test_upload_without_a_known_workspace_is_refused(
    tmp_path: Path, unreadable: bool
) -> None:
    """
    The runtime cwd alone is not trusted as a boundary: without the runner's
    registry, or when the session's workspace could not be read, nothing is read
    or posted, even though the registry's default root would hold the file.

    :param tmp_path: Pytest-provided runtime cwd holding the requested file.
    :param unreadable: Whether the session's workspace lookup failed, with a
        registry rooted at the runtime cwd, rather than no registry at all.
    """
    (tmp_path / "notes.txt").write_text("notes")

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await _upload(
            client,
            "notes.txt",
            agent_spec=_placeholder_cwd_spec(),
            resource_registry=(
                SessionResourceRegistry(runner_workspace=tmp_path) if unreadable else None
            ),
            runner_workspace=tmp_path,
            session_workspace=UNKNOWN_SESSION_WORKSPACE if unreadable else None,
        )
    finally:
        await client.aclose()

    assert result == "Error: sys_files_upload failed: the session workspace is unknown"
    assert captured == []


@pytest.mark.asyncio
async def test_async_upload_keeps_the_session_workspace(tmp_path: Path) -> None:
    """
    ``sys_call_async`` hands the session's own workspace to its upload target, so a
    background upload reads the same files as a direct one.

    :param tmp_path: Pytest-provided session workspace.
    """
    (tmp_path / "report.txt").write_text("async report")
    posted: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/policies/evaluate"):
            return httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW"})
        posted.append(request)
        return httpx.Response(201, json={"id": "file_async"})

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://ap-server"
    ) as client:
        handle = await execute_tool(
            tool_name="sys_call_async",
            arguments=json.dumps(
                {"tool": "sys_files_upload", "args": json.dumps({"path": "report.txt"})}
            ),
            server_client=client,
            agent_spec=AgentSpec(spec_version=1, skills_filter="none", tool_groups=_FILE_TOOLS_ON),
            conversation_id=_CONVERSATION_ID,
            session_workspace=tmp_path,
            session_inbox=inbox,
            session_async_tasks=tasks,
        )
        await tasks[json.loads(handle)["handle_id"]][0]
        completion = await inbox.get()

    assert completion["status"] == "completed", completion
    assert json.loads(completion["output"])["file_id"] == "file_async"
    assert len(posted) == 1
    assert b"async report" in posted[0].content


@pytest.mark.parametrize("old_name", ["upload_file", "list_files", "download_file"])
@pytest.mark.asyncio
async def test_unprefixed_file_tool_names_are_not_dispatched(
    old_name: str,
    tmp_path: Path,
) -> None:
    """
    The file tools' former names are not runner tools and are not granted even
    with the files group on, so a model calling one is refused without any
    request to the session file store.

    :param old_name: A pre-``sys_`` file tool name, e.g. ``"upload_file"``.
    :param tmp_path: Pytest-provided workspace root for the session.
    """
    (tmp_path / "notes.txt").write_text("notes")

    captured: list[httpx.Request] = []
    client = _recording_client(captured)
    try:
        result = await execute_tool(
            tool_name=old_name,
            arguments=json.dumps({"path": "notes.txt", "file_id": "file_abc123"}),
            server_client=client,
            agent_spec=AgentSpec(spec_version=1, skills_filter="none", tool_groups=_FILE_TOOLS_ON),
            conversation_id=_CONVERSATION_ID,
            runner_workspace=tmp_path,
        )
    finally:
        await client.aclose()

    assert not should_dispatch_locally(old_name)
    assert "not enabled for this agent" in json.loads(result)["error"]
    assert captured == []


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
    mapping: dict[str, dict[str, Any]],
    existing_child: dict[str, Any] | None = None,
    copy_status: int = 200,
    copy_error: dict[str, Any] | None = None,
    deletes: list[str] | None = None,
    delete_status: int = 200,
    delete_exc: httpx.HTTPError | None = None,
    events_status: int = 202,
    meta_gets: list[str] | None = None,
):
    """
    Build a mock Omnigent-server handler for a fresh named spawn.

    Serves the no-existing-child lookup, the child create, the copy
    endpoint (recording its body and answering with ``mapping`` or an
    error), and the child events POST (recording its body). The enriched
    copy response now carries filename + content_type per file, so the
    dispatch path needs no per-file metadata GET — any such GET is recorded
    in ``meta_gets`` so a test can assert it never happens.

    :param events: List the handler appends each child events body to.
    :param copies: List the handler appends each copy request body to.
    :param mapping: ``{old_id: {new_id, filename, content_type}}`` returned
        by the copy endpoint.
    :param existing_child: Optional child-session summary returned by the
        named-child lookup.
    :param copy_status: HTTP status the copy endpoint returns.
    :param copy_error: Error body for a non-2xx copy response.
    :param delete_status: HTTP status the child-session delete returns.
    :param delete_exc: Optional HTTP error raised by the delete request.
    :param meta_gets: If provided, the handler records the path of any
        per-file metadata GET here (expected to stay empty).
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_ID}/child_sessions":
            return httpx.Response(
                200,
                json={"data": [existing_child] if existing_child is not None else []},
            )
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
            # The enriched copy response makes this GET unnecessary; record it
            # so a test can prove the dispatch path no longer issues it.
            if meta_gets is not None:
                meta_gets.append(path)
            return httpx.Response(404, json={"error": {"message": "should not be called"}})
        if request.method == "POST" and path == f"/v1/sessions/{_CHILD_ID}/events":
            events.append(json.loads(request.content))
            if events_status >= 400:
                return httpx.Response(events_status, json={"error": {"message": "boom"}})
            return httpx.Response(202, json={"queued": True})
        if request.method == "DELETE" and path == f"/v1/sessions/{_CHILD_ID}":
            if deletes is not None:
                deletes.append(_CHILD_ID)
            if delete_exc is not None:
                raise delete_exc
            return httpx.Response(
                delete_status,
                json={"id": _CHILD_ID, "deleted": delete_status < 400},
            )
        return httpx.Response(404, json={"error": str(request.url)})

    return _handler


def _copied(new_id: str, filename: str, content_type: str | None) -> dict[str, Any]:
    """An enriched copy-response mapping entry for one file."""
    return {"new_id": new_id, "filename": filename, "content_type": content_type}


async def _run_spawn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    args_payload: Any,
    handler,
) -> str:
    """Dispatch one ``sys_session_send`` against ``handler`` and clean up."""
    from omnigent.runner import app as runner_app

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(subagent_work, "register_child_session", lambda *a, **k: None)
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
            subagent_work.unregister_child_session(_CHILD_ID)
            subagent_work.unregister_subagent_work(_CHILD_ID)
            subagent_work._session_inboxes_ref.pop(_PARENT_ID, None)


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
    The content type comes from the copy response, so no per-file metadata
    GET is issued.
    """
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    meta_gets: list[str] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_parent": _copied("file_child", "notes.txt", "text/plain")},
        meta_gets=meta_gets,
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
    # The enriched copy response carried the content type, so the dispatch
    # path never fetched per-file metadata.
    assert meta_gets == [], "content type comes from the copy response — no metadata GET"


@pytest.mark.asyncio
async def test_send_with_image_file_id_attaches_input_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``image/*`` content type yields an ``input_image`` block."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_pic": _copied("file_child_pic", "chart.png", "image/png")},
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
async def test_send_image_file_falls_back_to_filename_when_no_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied file with no content_type falls back to the filename guess."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_pic": _copied("file_child_pic", "chart.png", None)},
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "look", "file_ids": ["file_pic"]},
        handler=handler,
    )

    assert json.loads(output)["status"] == "launching"
    content = events[0]["data"]["content"]
    # No content_type on the copy row → filename ".png" drives the split.
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
        mapping={
            "f_a": _copied("c_a", "a.pdf", "application/pdf"),
            "f_b": _copied("c_b", "b.png", "image/png"),
            "f_c": _copied("c_c", "c.csv", "text/csv"),
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
async def test_send_by_session_id_rejects_file_ids_before_server_call() -> None:
    """By-session-id sends cannot forward files."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected server call: {request.method} {request.url}")

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "session_id": _CHILD_ID,
                        "args": {"input": "continue", "file_ids": ["file_parent"]},
                    }
                ),
                server_client=server_client,
                conversation_id=_PARENT_ID,
                agent_spec=_spec_with_subagent(),
                session_inbox=session_inbox,
            )
        finally:
            subagent_work._session_inboxes_ref.pop(_PARENT_ID, None)

    assert "file_ids" in output
    assert "existing session by id" in output


@pytest.mark.parametrize(
    "file_ids",
    [
        [],
        ["file_parent", "file_parent"],
    ],
)
@pytest.mark.asyncio
async def test_send_rejects_invalid_file_ids_before_server_call(
    file_ids: list[str],
) -> None:
    """Malformed file_ids are rejected before child lookup or copy."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected server call: {request.method} {request.url}")

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "task-1",
                        "args": {"input": "spawn", "file_ids": file_ids},
                    }
                ),
                server_client=server_client,
                conversation_id=_PARENT_ID,
                agent_spec=_spec_with_subagent(),
                session_inbox=session_inbox,
            )
        finally:
            subagent_work._session_inboxes_ref.pop(_PARENT_ID, None)

    assert output.startswith("Error: sys_session_send invalid 'file_ids':"), output


@pytest.mark.asyncio
async def test_send_named_continuation_rejects_file_ids_before_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named sends only copy files when they create the child session."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_parent": _copied("file_child", "notes.txt", "text/plain")},
        existing_child={
            "id": _CHILD_ID,
            "title": "worker:task-1",
            "tool": "worker",
            "session_name": "task-1",
            "busy": False,
            "labels": {},
        },
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "continue", "file_ids": ["file_parent"]},
        handler=handler,
    )

    assert "file_ids" in output
    assert "already exists" in output
    assert copies == []
    assert events == []


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


@pytest.mark.asyncio
async def test_copy_failure_surfaces_child_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed spawn reports when teardown cannot delete the child."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    deletes: list[str] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={},
        copy_status=404,
        copy_error={"error": {"message": "File 'file_bogus' not found in source session"}},
        deletes=deletes,
        delete_status=500,
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "use this", "file_ids": ["file_bogus"]},
        handler=handler,
    )

    assert output.startswith("Error: failed to copy files to child:"), output
    assert "Warning: failed to delete newly-created child session" in output
    assert "500" in output
    assert events == []
    assert deletes == [_CHILD_ID, _CHILD_ID]


@pytest.mark.asyncio
async def test_send_message_failure_after_copy_tears_down_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A copy that succeeds but a child events POST that then fails must
    tear down the freshly-created child — the copy already wrote
    child-scoped file rows, so leaving the child behind would orphan
    those rows and poison a same-(agent, title) retry with a phantom.
    """
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    deletes: list[str] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_parent": _copied("file_child", "notes.txt", "text/plain")},
        deletes=deletes,
        events_status=500,
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "use this", "file_ids": ["file_parent"]},
        handler=handler,
    )

    assert output.startswith("Error: failed to send message to child:"), output
    assert "500" in output
    # Copy happened and the event was attempted, but the failure must
    # delete the child (which reclaims the copied file rows with it).
    assert len(copies) == 1
    assert len(events) == 1
    assert deletes == [_CHILD_ID], "send failure after copy must delete the child"


@pytest.mark.asyncio
async def test_send_message_failure_surfaces_child_delete_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message-post failure reports teardown transport failures too."""
    events: list[dict[str, Any]] = []
    copies: list[dict[str, Any]] = []
    deletes: list[str] = []
    handler = _spawn_server_handler(
        events=events,
        copies=copies,
        mapping={"file_parent": _copied("file_child", "notes.txt", "text/plain")},
        deletes=deletes,
        delete_exc=httpx.ConnectError("delete failed"),
        events_status=500,
    )

    output = await _run_spawn(
        monkeypatch,
        args_payload={"input": "use this", "file_ids": ["file_parent"]},
        handler=handler,
    )

    assert output.startswith("Error: failed to send message to child:"), output
    assert "Warning: failed to delete newly-created child session" in output
    assert "ConnectError: delete failed" in output
    assert len(copies) == 1
    assert len(events) == 1
    assert deletes == [_CHILD_ID, _CHILD_ID]
