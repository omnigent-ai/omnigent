"""Tests for runner-side environment filesystem endpoints (Phase 3)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.entities.environment_filesystem import FilesystemPathNotFound
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests.runner.helpers import NullServerClient


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with test files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.txt").write_text("hello world")
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_text("print('hi')")
    # A binary file (PNG signature + a NUL) that is not valid UTF-8, used to
    # exercise the base64 binary-read path.
    (ws / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff")
    return ws


@pytest.fixture
def registry(workspace: Path) -> SessionResourceRegistry:
    """Registry with a real CallerProcessOSEnvironment."""
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    reg = SessionResourceRegistry()
    reg._primary_envs["conv_test"] = os_env
    return reg


@pytest.fixture
def app(registry: SessionResourceRegistry, workspace: Path) -> FastAPI:
    """Runner app with the registry."""
    return create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client for the runner app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runner",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_list_environment_root(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem lists root directory entries."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    names = {e["name"] for e in body["data"]}
    assert "hello.txt" in names
    assert "src" in names


@pytest.mark.asyncio
async def test_list_environment_root_with_broken_symlink(
    tmp_path: Path,
) -> None:
    """GET /filesystem succeeds even when the workspace contains a broken symlink.

    Broken symlinks are common in large repos (e.g. Bazel convenience symlinks
    like bazel-out/bazel-bin pointing to a cleaned or absent cache).  The old
    os.stat()-based implementation would raise FileNotFoundError and fail the
    entire listing; the new implementation uses os.lstat() so broken symlinks
    are listed as file-type entries rather than crashing the whole request.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "real.txt").write_text("hello")
    # Create a symlink that points to a non-existent target.
    (ws / "broken_link").symlink_to(ws / "does_not_exist")

    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(ws), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert os_env is not None
    reg = SessionResourceRegistry()
    reg._primary_envs["conv_broken"] = os_env
    app = create_runner_app(
        resource_registry=reg,
        runner_workspace=ws,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.get(
            f"/v1/sessions/conv_broken/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "list"
    names = {e["name"] for e in body["data"]}
    # The real file is visible.
    assert "real.txt" in names
    # Broken symlinks are listed as file-type entries via the lstat fallback
    # rather than being silently skipped or crashing. Size is None because
    # os.stat() (which follows symlinks) fails and lstat() on the symlink
    # itself doesn't reflect the target's size.
    assert "broken_link" in names
    broken_entry = next(e for e in body["data"] if e["name"] == "broken_link")
    assert broken_entry["type"] == "file"
    assert broken_entry["bytes"] is None


@pytest.mark.asyncio
async def test_list_subdirectory(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem/src lists the src directory."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem/src"
    )
    assert resp.status_code == 200
    body = resp.json()
    names = {e["name"] for e in body["data"]}
    assert "main.py" in names


@pytest.mark.asyncio
async def test_read_file_content(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem/hello.txt returns file content."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.filesystem.file_content"
    assert body["content"] == "hello world"
    assert body["encoding"] == "utf-8"
    assert body["bytes"] == 11


@pytest.mark.asyncio
async def test_read_binary_file_content(
    client: httpx.AsyncClient,
) -> None:
    """A non-UTF-8 file is returned whole as base64, not truncated text."""
    import base64

    raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff"
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/logo.png"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["encoding"] == "base64"
    assert body["content_type"] == "image/png"
    # The base64 payload round-trips to the exact original bytes — no
    # UTF-8 replacement-char corruption.
    assert base64.b64decode(body["content"]) == raw
    assert body["bytes"] == len(raw)
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_write_file(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """PUT /filesystem/new.txt creates a file."""
    resp = await client.put(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/new.txt",
        json={"content": "new content", "encoding": "utf-8"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.filesystem.write_result"
    assert body["created"] is True
    assert body["bytes_written"] == 11
    assert (workspace / "new.txt").read_text() == "new content"


@pytest.mark.asyncio
async def test_edit_file(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """PATCH /filesystem/hello.txt edits a file."""
    resp = await client.patch(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt",
        json={"old_text": "hello", "new_text": "goodbye"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.filesystem.edit_result"
    assert body["replacements"] == 1
    assert (workspace / "hello.txt").read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_delete_file(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """DELETE /filesystem/hello.txt deletes a file."""
    resp = await client.delete(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.filesystem.delete_result"
    assert body["deleted"] is True
    assert not (workspace / "hello.txt").exists()


@pytest.mark.asyncio
async def test_delete_nonempty_directory_requires_recursive(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /filesystem/src without recursive=true returns 409."""
    resp = await client.delete(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem/src"
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "directory_not_empty"


@pytest.mark.asyncio
async def test_delete_directory_recursive(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """DELETE /filesystem/src?recursive=true deletes the directory."""
    resp = await client.delete(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/src?recursive=true"
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not (workspace / "src").exists()


# ── shell-injection regression for stat/_stat_via_shell/_check_dir_empty ──


@pytest.mark.asyncio
async def test_delete_path_with_command_substitution_does_not_execute(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """A DELETE path containing ``$(...)`` must not execute the substituted command.

    Regression test: the DELETE handler reaches ``_stat_via_shell``
    and ``_check_dir_empty``, which used to interpolate the caller-controlled
    path into a double-quoted ``python3 -c`` script. The shell expanded
    ``$(...)`` before python ran, giving arbitrary command execution.

    The payload ``inj$(touch PWNED)x`` would, on the vulnerable code, run
    ``touch PWNED`` in the workspace (cwd of the helper). With the path
    embedded as a Python literal via ``json.dumps`` and the whole script
    shell-quoted, the string is statted verbatim — no command runs.
    """
    import urllib.parse

    # The marker the injected command would create if substitution fired.
    marker = workspace / "PWNED"
    assert not marker.exists(), "Precondition: marker must not exist before the request."

    payload = "inj$(touch PWNED)x"
    encoded = urllib.parse.quote(payload, safe="")
    resp = await client.delete(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/{encoded}"
    )

    # The literal path does not exist, so the stat fails and the endpoint
    # returns 404 — on BOTH old and new code the HTTP status is 404 (the
    # substituted command produced empty output, leaving "injx"). The marker
    # is the discriminator: it is created only if the injection executed.
    assert resp.status_code == 404, (
        f"Expected 404 for a non-existent literal path, got {resp.status_code}. Body: {resp.text}"
    )
    assert not marker.exists(), (
        "Injected command executed: the 'PWNED' marker was created. The path "
        "reached a shell-interpreted context (shell-injection regression)."
    )


@pytest.mark.asyncio
async def test_delete_real_file_with_command_substitution_name(
    client: httpx.AsyncClient,
    workspace: Path,
) -> None:
    """A real file whose name literally contains ``$(...)`` can be deleted.

    Usability guard for the shell-injection fix: embedding the path as a Python
    literal must treat shell metacharacters as ordinary filename bytes, so a
    legitimately (if unusually) named file is statted and removed correctly.

    If the path were still shell-interpreted, ``$(whoami)`` would expand and
    ``os.stat`` would receive a *different* string (``data<user>.txt``), the
    stat would miss, and the real file would be left on disk — surfacing as a
    404 and a surviving file. Asserting a successful delete of the real file
    proves the verbatim path made it through.
    """
    import urllib.parse

    weird_name = "data$(whoami).txt"
    target = workspace / weird_name
    target.write_text("payload-bytes")
    assert target.exists(), "Precondition: the metacharacter-named file must exist."

    encoded = urllib.parse.quote(weird_name, safe="")
    resp = await client.delete(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/{encoded}"
    )

    assert resp.status_code == 200, (
        f"Expected 200 deleting a real metacharacter-named file, got "
        f"{resp.status_code}. A non-200 means the literal path was not used. "
        f"Body: {resp.text}"
    )
    body = resp.json()
    assert body["deleted"] is True, "The endpoint must report the file as deleted."
    assert body["type"] == "file", "The metacharacter-named entry is a regular file."
    # The verbatim-named file is gone — proves stat + rm both used the literal path.
    assert not target.exists(), (
        f"File {weird_name!r} still on disk after a reported delete; the rm "
        "did not target the literal path."
    )


@pytest.mark.asyncio
async def test_stat_path_with_command_substitution_does_not_execute(
    workspace: Path,
) -> None:
    """``CallerProcessFilesystem.stat`` must not execute ``$(...)`` in the path.

    ``stat`` shares the same shell-injection flaw but is not wired to a GET route, so it
    is exercised here through its public method. A non-existent literal path
    containing a command substitution must raise ``FilesystemPathNotFound``
    without creating the marker the substituted command would produce.
    """
    from omnigent.runner.environment_filesystem import CallerProcessFilesystem

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    fs = CallerProcessFilesystem(os_env)

    marker = workspace / "PWNED_STAT"
    assert not marker.exists(), "Precondition: stat marker must not exist."

    with pytest.raises(FilesystemPathNotFound):
        await fs.stat("inj$(touch PWNED_STAT)x")

    assert not marker.exists(), (
        "stat executed the injected command: 'PWNED_STAT' marker was created "
        "(shell-injection regression in stat())."
    )


@pytest.mark.asyncio
async def test_stat_real_file_with_command_substitution_name(
    workspace: Path,
) -> None:
    """``CallerProcessFilesystem.stat`` returns correct metadata for a file whose
    name literally contains ``$(...)``.

    Usability guard: the verbatim filename must be statted, returning a
    file-type entry with the real byte size. A shell-interpreted path would
    stat a different string and raise instead.
    """
    from omnigent.runner.environment_filesystem import CallerProcessFilesystem

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    fs = CallerProcessFilesystem(os_env)

    weird_name = "report$(id).txt"
    contents = "twelve bytes"
    (workspace / weird_name).write_text(contents)

    entry = await fs.stat(weird_name)

    # type/name/bytes prove the literal path was statted, not a shell-expanded
    # variant (which would have raised FilesystemPathNotFound instead).
    assert entry.type == "file", f"Expected a file entry, got type={entry.type!r}."
    assert entry.name == weird_name, (
        f"Expected name {weird_name!r}, got {entry.name!r}; the path was not passed verbatim."
    )
    assert entry.bytes == len(contents.encode("utf-8")), (
        f"Expected {len(contents.encode('utf-8'))} bytes, got {entry.bytes}; "
        "stat read the wrong path."
    )


@pytest.mark.asyncio
async def test_path_traversal_rejected(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem with traversal component returns 400."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/" + "..%2F..%2Fetc%2Fpasswd"
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_read_nonexistent_file_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem/nope.txt returns 404."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/nope.txt"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "path_not_found"


@pytest.mark.asyncio
async def test_filesystem_session_without_agent_id_returns_typed_404(
    registry: SessionResourceRegistry,
) -> None:
    """
    Missing ``agent_id`` in a session snapshot returns a typed 404.

    :param registry: Registry with a real caller-process environment.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a malformed session snapshot with no ``agent_id``.

        :param request: Runner request for the session snapshot.
        :returns: HTTP response with malformed session JSON.
        """
        assert request.url.path == "/v1/sessions/conv_test"
        return httpx.Response(200, json={"id": "conv_test"})

    async def _resolver(agent_id: str, session_id: str | None = None) -> None:
        """
        Fail if the runner tries to resolve a missing agent id.

        :param agent_id: Agent id requested by the runner.
        """
        raise AssertionError(f"unexpected resolver call for {agent_id}")

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        resource_registry=registry,
        server_client=server_client,
        spec_resolver=_resolver,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        server_client,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://runner",
        ) as client,
    ):
        resp = await client.get(
            f"/v1/sessions/conv_test/resources/environments"
            f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt"
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == (
        "session spec resolver: session 'conv_test' has no agent_id"
    )


@pytest.mark.asyncio
async def test_filesystem_missing_session_agent_returns_typed_404(
    registry: SessionResourceRegistry,
) -> None:
    """
    Missing session agent spec returns a typed 404.

    :param registry: Registry with a real caller-process environment.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a session snapshot with an unresolved ``agent_id``.

        :param request: Runner request for the session snapshot.
        :returns: HTTP response with an agent reference.
        """
        assert request.url.path == "/v1/sessions/conv_test"
        return httpx.Response(200, json={"id": "conv_test", "agent_id": "ag_missing"})

    async def _resolver(agent_id: str, session_id: str | None = None) -> None:
        """
        Return no spec for the requested agent id.

        :param agent_id: Agent id requested by the runner.
        :returns: ``None`` to indicate no spec was found.
        """
        assert agent_id == "ag_missing"
        return

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        resource_registry=registry,
        server_client=server_client,
        spec_resolver=_resolver,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        server_client,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://runner",
        ) as client,
    ):
        resp = await client.get(
            f"/v1/sessions/conv_test/resources/environments"
            f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt"
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == (
        "session spec resolver: agent 'ag_missing' for session 'conv_test' was not found"
    )


# ── Phase 5: shell endpoint ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_echo(
    client: httpx.AsyncClient,
) -> None:
    """POST /shell runs a command and returns structured output."""
    resp = await client.post(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/shell",
        json={"command": "echo hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.shell_result"
    assert body["stdout"].strip() == "hello"
    assert body["exit_code"] == 0
    assert body["timed_out"] is False


@pytest.mark.asyncio
async def test_shell_nonzero_exit(
    client: httpx.AsyncClient,
) -> None:
    """POST /shell returns non-zero exit code on failure."""
    resp = await client.post(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/shell",
        json={"command": "exit 42"},
    )
    assert resp.status_code == 200
    assert resp.json()["exit_code"] == 42


@pytest.mark.asyncio
async def test_shell_missing_command_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """POST /shell without command returns 400."""
    resp = await client.post(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/shell",
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_read_file_content_type_python(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem/src/main.py includes content_type for Python files."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/src/main.py"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.filesystem.file_content"
    # mimetypes.guess_type returns text/x-python or text/plain for .py
    assert body["content_type"] is not None
    assert "python" in body["content_type"] or "text" in body["content_type"]


@pytest.mark.asyncio
async def test_read_file_content_type_text(
    client: httpx.AsyncClient,
) -> None:
    """GET /filesystem/hello.txt includes content_type for .txt files."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/filesystem/hello.txt"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_type"] == "text/plain"


@pytest.fixture
def app_with_registry(
    registry: SessionResourceRegistry,
    workspace: Path,
) -> FastAPI:
    """Runner app whose internal filesystem registry is pre-seeded with changes.

    The runner always owns its registry; tests reach it via
    ``app.state.filesystem_registry`` and inject events directly so
    the watchdog observer does not need to be started.

    :param registry: Session resource registry with the test environment.
    :param workspace: Test workspace directory passed as runner_workspace
        so the registry watches the right root.
    :returns: FastAPI runner app with seeded change events.
    """
    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    reg = app.state.filesystem_registry
    # Seed change events via the public API: hello.txt modified, gone.py deleted.
    reg.record_change("hello.txt", "modified", "conv_test")
    reg.record_change("gone.py", "deleted", "conv_test")
    return app


@pytest.fixture
async def client_with_registry(
    app_with_registry: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client for the registry-backed runner app.

    :param app_with_registry: The runner app with filesystem registry.
    :returns: Async HTTP client.
    """
    transport = httpx.ASGITransport(app=app_with_registry)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runner",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_filesystem_changes_modified(
    client_with_registry: httpx.AsyncClient,
) -> None:
    """The /changes endpoint returns modified files.

    The registry has hello.txt seeded as 'modified'; the changes
    endpoint must return it with status='modified'.
    """
    resp = await client_with_registry.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/changes"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    entries_by_path = {e["path"]: e for e in body["data"]}
    assert "hello.txt" in entries_by_path, "Modified file must appear in changes"
    assert entries_by_path["hello.txt"]["status"] == "modified"


@pytest.mark.asyncio
async def test_filesystem_changes_deleted(
    client_with_registry: httpx.AsyncClient,
) -> None:
    """The /changes endpoint returns deleted files.

    gone.py was deleted during the session; it must appear in the
    changes list with status='deleted' even though it's absent on disk.
    """
    resp = await client_with_registry.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/changes"
    )
    assert resp.status_code == 200
    body = resp.json()
    entries_by_path = {e["path"]: e for e in body["data"]}
    assert "gone.py" in entries_by_path, "Deleted file must appear in changes"
    assert entries_by_path["gone.py"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_filesystem_changes_no_events_returns_empty(
    client: httpx.AsyncClient,
) -> None:
    """A session with no seeded change events returns an empty list.

    The baseline client fixture uses a runner app whose filesystem registry
    has no events recorded for the session; the endpoint must return 200
    with an empty data array rather than an error.
    """
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/changes"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == [], "No registry means no changes"


# ── _require_os_env guard: all Phase-3 endpoints ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path_suffix,json_body",
    [
        ("GET", "/filesystem", None),
        ("GET", "/changes", None),
        ("GET", "/filesystem/hello.txt", None),
        ("PUT", "/filesystem/new.txt", {"content": "x", "encoding": "utf-8"}),
        (
            "PATCH",
            "/filesystem/hello.txt",
            {"edits": [{"old_text": "hello world", "new_text": "bye"}]},
        ),
        ("DELETE", "/filesystem/hello.txt", None),
        ("POST", "/shell", {"command": "echo hi"}),
        ("GET", "/diff/hello.txt", None),
        ("GET", "/search?q=hello", None),
    ],
    ids=[
        "list_root",
        "list_changes",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "shell_exec",
        "diff_file",
        "search_files",
    ],
)
async def test_filesystem_endpoints_return_404_when_spec_has_no_os_env(
    registry: SessionResourceRegistry,
    method: str,
    path_suffix: str,
    json_body: dict | None,
) -> None:
    """All Phase-3 filesystem/shell endpoints return 404 when the agent spec
    has no os_env.

    The ``_require_os_env`` guard in the runner must fire before any
    environment resolution so no synthetic filesystem is exposed.  A 404
    is the correct status: the resource conceptually does not exist for
    this session.

    :param registry: Registry with a real caller-process environment
        (the guard fires before the registry is consulted).
    :param method: HTTP method for the endpoint under test.
    :param path_suffix: URL suffix after the environment id segment.
    :param json_body: Optional JSON request body.
    """

    async def _session_handler(request: httpx.Request) -> httpx.Response:
        """Return a session snapshot pointing at a no-os_env agent."""
        assert request.url.path == "/v1/sessions/conv_test"
        return httpx.Response(200, json={"id": "conv_test", "agent_id": "ag_no_env"})

    async def _resolver(agent_id: str, session_id: str | None = None) -> object:
        """Return a spec with no os_env for the agent under test."""
        assert agent_id == "ag_no_env"
        return SimpleNamespace(os_env=None)

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_session_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        resource_registry=registry,
        server_client=server_client,
        spec_resolver=_resolver,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    base = f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
    async with (
        server_client,
        httpx.AsyncClient(transport=transport, base_url="http://runner") as client,
    ):
        resp = await client.request(method, base + path_suffix, json=json_body)

    # 404 — the guard fires before any env resolution; the resource does
    # not exist for a spec with no os_env.  Any other status (200/500)
    # would mean the guard was bypassed.
    assert resp.status_code == 404, (
        f"{method} {path_suffix} returned {resp.status_code}, "
        f"expected 404 from _require_os_env guard. "
        f"Body: {resp.text}"
    )
    # The detail message must mention os_env so callers can diagnose the issue.
    assert "no os_env" in resp.json().get("detail", ""), (
        f"Expected 'no os_env' in detail, got: {resp.json()}"
    )


# ── diff endpoint ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diff_endpoint_returns_before_and_after_for_modified_file(
    app_with_registry: FastAPI,
) -> None:
    """GET /diff/hello.txt returns before=snapshot and after=current-disk-content for
    a modified file.

    :param app_with_registry: Runner app with hello.txt seeded as 'modified'.
    """

    reg = app_with_registry.state.filesystem_registry
    # Seed the pre-modification snapshot so get_baseline has something to return.
    # This simulates what the PUT/PATCH handlers do before overwriting the file.
    reg.seed_snapshot("hello.txt", "before content")

    transport = httpx.ASGITransport(app=app_with_registry)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.get(
            f"/v1/sessions/conv_test/resources/environments"
            f"/{DEFAULT_ENVIRONMENT_ID}/diff/hello.txt"
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}. Body: {resp.text}"
    body = resp.json()
    # Confirm the response carries the correct object type.
    assert body["object"] == "session.environment.filesystem.file_diff", (
        f"Expected object 'session.environment.filesystem.file_diff', got {body['object']!r}."
    )
    # before must be the snapshot we seeded — proves get_baseline returns the snapshot.
    assert body["before"] == "before content", (
        f"Expected before='before content', got {body['before']!r}. "
        "seed_snapshot or get_baseline is not wiring the snapshot through."
    )
    # after must be the current on-disk content — proves the endpoint reads from disk.
    assert body["after"] == "hello world", (
        f"Expected after='hello world', got {body['after']!r}. "
        "The diff endpoint is not reading current file content from disk."
    )


@pytest.mark.asyncio
async def test_diff_endpoint_returns_null_before_for_new_file(
    registry: SessionResourceRegistry,
    workspace: Path,
) -> None:
    """GET /diff/hello.txt returns before=None when the file is new (created event, no snapshot).

    A 'created' event with no seed_snapshot means no baseline exists;
    ``get_baseline`` should return ``None`` which flows through as ``before: null``.

    :param registry: Session resource registry backed by the test workspace.
    :param workspace: Test workspace; hello.txt on disk contains 'hello world'.
    """
    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reg = app.state.filesystem_registry
    # Inject a 'created' event but deliberately skip seed_snapshot so before=None.
    reg.record_change("hello.txt", "created", "conv_test")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.get(
            f"/v1/sessions/conv_test/resources/environments"
            f"/{DEFAULT_ENVIRONMENT_ID}/diff/hello.txt"
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}. Body: {resp.text}"
    body = resp.json()
    # No snapshot → before must be null; a non-null value means a phantom
    # baseline was produced.
    assert body["before"] is None, (
        f"Expected before=None for a new file with no snapshot, got {body['before']!r}."
    )
    # File exists on disk → after must be its content.
    assert body["after"] == "hello world", f"Expected after='hello world', got {body['after']!r}."


@pytest.mark.asyncio
async def test_diff_endpoint_returns_null_after_for_deleted_file(
    client_with_registry: httpx.AsyncClient,
) -> None:
    """GET /diff/gone.py returns after=None for a deleted file.

    ``app_with_registry`` seeds gone.py with a 'deleted' event; the
    endpoint must set after=None rather than trying to read a file that
    no longer exists.

    :param client_with_registry: HTTP client backed by the registry-seeded app.
    """
    resp = await client_with_registry.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/diff/gone.py"
    )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}. Body: {resp.text}"
    body = resp.json()
    # Deleted file → after must be null; a non-null value means the endpoint
    # tried to read a file that no longer exists instead of short-circuiting.
    assert body["after"] is None, f"Expected after=None for deleted file, got {body['after']!r}."


@pytest.mark.asyncio
async def test_diff_endpoint_returns_404_when_file_not_in_registry(
    client_with_registry: httpx.AsyncClient,
) -> None:
    """GET /diff/not_changed.txt returns 404 when the path has no change event.

    The registry has no event for 'not_changed.txt'; the endpoint must
    return 404 with error.code='not_found' before attempting any file read.

    :param client_with_registry: HTTP client backed by the registry-seeded app.
    """
    resp = await client_with_registry.get(
        f"/v1/sessions/conv_test/resources/environments"
        f"/{DEFAULT_ENVIRONMENT_ID}/diff/not_changed.txt"
    )

    assert resp.status_code == 404, (
        f"Expected 404 for a file not in the registry, got {resp.status_code}. Body: {resp.text}"
    )
    body = resp.json()
    # error.code must be 'not_found' — confirms the typed error path fires.
    assert body["error"]["code"] == "not_found", (
        f"Expected error.code='not_found', got {body['error']['code']!r}."
    )


@pytest.mark.asyncio
async def test_diff_endpoint_returns_full_after_for_large_file(
    registry: SessionResourceRegistry,
    workspace: Path,
) -> None:
    """GET /diff/<large_file> returns the complete file content in ``after``,
    not just the first 2 000 lines.

    Regression test: the original implementation read ``after`` via
    ``CallerProcessFilesystem.read`` without a ``limit`` argument, which defaulted
    to ``_DEFAULT_READ_LIMIT = 2 000`` lines — the cap that protects LLM context
    windows from unbounded files.  That cap is wrong for diff rendering (the UI
    needs the whole file).  The fix passes ``limit=None`` to
    ``CallerProcessFilesystem.read``, which now means "no line cap".

    This test fails when the 2 000-line cap is re-introduced: ``returned_lines``
    would be 2 000 instead of 3 000.

    :param registry: Session resource registry backed by the test workspace.
    :param workspace: Test workspace directory (not a git repo, so
        ``AgentEditFilesystemRegistry`` is used).
    """
    # 3 000 lines — comfortably above the 2 000-line agent-tool cap.
    total_lines = 3_000
    large_content = "\n".join(f"line {i}" for i in range(1, total_lines + 1)) + "\n"
    (workspace / "large.py").write_text(large_content)

    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reg = app.state.filesystem_registry
    reg.record_change("large.py", "modified", "conv_test")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.get(
            f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/diff/large.py"
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}. Body: {resp.text}"
    body = resp.json()
    after = body["after"]
    assert after is not None, "Expected after to be non-null for an existing file on disk."
    returned_lines = len(after.splitlines())
    # The file has 3 000 lines. If the old 2 000-line cap is still applied,
    # this would be 2 000. The fix must return all 3 000.
    assert returned_lines == total_lines, (
        f"Expected after to contain all {total_lines} lines, got {returned_lines}. "
        f"If {returned_lines} == 2000, the agent-tool line cap from "
        "CallerProcessFilesystem.read is still being applied to the diff endpoint."
    )
    # Verify the last line is present to rule out off-by-one coincidences.
    assert f"line {total_lines}" in after, (
        f"Expected 'line {total_lines}' to appear in after, "
        "but the content was cut off before the end of the file."
    )


# ── /search endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "q,expected_path,failure_hint",
    [
        (
            "main",
            "src/main.py",
            "Search endpoint not walking subdirectories (q='main' should find src/main.py).",
        ),
        (
            "hell",
            "hello.txt",
            "Filename substring matching is broken (q='hell' should find hello.txt).",
        ),
        (
            "src/main",
            "src/main.py",
            "Full-path substring matching is broken (q='src/main' should find src/main.py).",
        ),
        (
            "HELLO",
            "hello.txt",
            "Search must be case-insensitive (q='HELLO' should find hello.txt).",
        ),
    ],
    ids=["subdir_walk", "filename_substring", "path_substring", "case_insensitive"],
)
async def test_search_matches(
    client: httpx.AsyncClient,
    q: str,
    expected_path: str,
    failure_hint: str,
) -> None:
    """GET /search?q=<q> returns the expected file.

    Covers four distinct matching requirements in one parametrized test:
    recursive walk into subdirectories, filename substring, full-path
    substring, and case-insensitive matching.

    :param client: httpx client for the runner app.
    :param q: Search query to send.
    :param expected_path: Relative path that must appear in the results.
    :param failure_hint: Explanation surfaced when the assertion fails.
    """
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?q={q}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    paths = {e["path"] for e in body["data"]}
    assert expected_path in paths, (
        f"Expected {expected_path!r} in search results for q={q!r}, got: {paths}. {failure_hint}"
    )


@pytest.mark.asyncio
async def test_search_no_matches_returns_empty_list(
    client: httpx.AsyncClient,
) -> None:
    """GET /search?q=zzznotfound returns an empty data array, not an error."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?q=zzznotfound"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    # No files match — the endpoint must return empty data, not 404 or 500.
    assert body["data"] == [], (
        f"Expected empty data for a query with no matches, got: {body['data']}"
    )


@pytest.mark.asyncio
async def test_search_returns_only_files_not_directories(
    client: httpx.AsyncClient,
) -> None:
    """GET /search results contain only file-type entries, not directory entries.

    The workspace has a 'src' directory; a query matching it by name must not
    return it as a result — directories are not useful for file-open actions.
    """
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?q=src"
    )
    assert resp.status_code == 200
    for entry in resp.json()["data"]:
        # Any directory entry in the results would indicate the search walk is
        # including directories, which the UI cannot open.
        assert entry["type"] == "file", (
            f"Expected only file-type entries in search results, "
            f"but got type={entry['type']!r} for path={entry['path']!r}."
        )


@pytest.mark.asyncio
async def test_search_result_entry_shape(
    client: httpx.AsyncClient,
) -> None:
    """Each /search result entry carries the expected fields for the UI."""
    resp = await client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?q=hello"
    )
    assert resp.status_code == 200
    entries = resp.json()["data"]
    entry = next(e for e in entries if e["path"] == "hello.txt")
    # object type must be the standard filesystem entry marker so the frontend
    # can deserialize it with the same mapper as listing responses.
    assert entry["object"] == "session.environment.filesystem.entry"
    assert entry["name"] == "hello.txt"
    assert entry["type"] == "file"
    # bytes and modified_at must be present (non-null) — the UI uses both for
    # display.  A None bytes would mean the stat call failed silently.
    assert entry["bytes"] is not None, (
        "bytes must be populated for an existing file; None means stat failed."
    )
    assert entry["modified_at"] is not None, (
        "modified_at must be populated for an existing file; None means stat failed."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url_suffix,description",
    [
        # q is required: a missing q must fail even when include is supplied —
        # include/exclude only narrow a query, they never search on their own.
        ("/search", "missing q"),
        ("/search?include=*.ts", "include without q"),
        # A provided-but-whitespace q is caught by the pattern validator.
        ("/search?q=%20%20%20", "whitespace-only q"),
    ],
    ids=["missing_q", "include_without_q", "whitespace_q"],
)
async def test_search_invalid_q_returns_422(
    client: httpx.AsyncClient,
    url_suffix: str,
    description: str,
) -> None:
    """GET /search requires a non-whitespace q and returns 422 otherwise.

    Whitespace-only queries (e.g. q=%20%20%20) would strip to "" in
    search_files() and match every file.  The API-layer required/pattern
    validator catches a missing or whitespace-only q before the handler runs,
    returning 422 — and an include glob alone is not a substitute for q.

    :param client: httpx client for the runner app.
    :param url_suffix: URL suffix including the (possibly invalid) query string.
    :param description: Human-readable description for assertion messages.
    """
    base = f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
    resp = await client.get(base + url_suffix)
    # FastAPI query-param validation fires before any handler code; a missing
    # or whitespace-only q returns 422, not 400 or 500.
    assert resp.status_code == 422, (
        f"Expected 422 for {description}, got {resp.status_code}. Body: {resp.text}"
    )


# ── /search include / exclude glob filters ───────────────────────────────────


@pytest.fixture
def glob_workspace(tmp_path: Path) -> Path:
    """Workspace with assorted file types/depths for glob filter tests.

    Layout::

        app.ts
        app.test.ts
        readme.md
        src/index.ts
        src/util.js
        src/deep/core.ts
        node_modules/pkg/index.ts

    :param tmp_path: pytest temp directory.
    :returns: Path to the populated workspace root.
    """
    ws = tmp_path / "globws"
    ws.mkdir()
    (ws / "app.ts").write_text("export const a = 1;")
    (ws / "app.test.ts").write_text("test('a', () => {});")
    (ws / "readme.md").write_text("# readme")
    (ws / "src").mkdir()
    (ws / "src" / "index.ts").write_text("export {};")
    (ws / "src" / "util.js").write_text("module.exports = {};")
    (ws / "src" / "deep").mkdir()
    (ws / "src" / "deep" / "core.ts").write_text("export {};")
    nm = ws / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.ts").write_text("export {};")
    return ws


@pytest.fixture
async def glob_client(glob_workspace: Path) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client for a runner app backed by ``glob_workspace``.

    :param glob_workspace: The populated workspace root.
    :returns: Async HTTP client for the runner app.
    """
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(glob_workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    reg = SessionResourceRegistry()
    reg._primary_envs["conv_test"] = os_env
    app = create_runner_app(
        resource_registry=reg,
        runner_workspace=glob_workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,expected,hint",
    [
        (
            "q=.&include=*.ts",
            {
                "app.ts",
                "app.test.ts",
                "src/index.ts",
                "src/deep/core.ts",
                "node_modules/pkg/index.ts",
            },
            "bare '*.ts' must match exactly the .ts files at any depth (no .js/.md)",
        ),
        (
            "q=.&include=src/**",
            {"src/index.ts", "src/util.js", "src/deep/core.ts"},
            "'src/**' must return exactly the files under the src subtree",
        ),
        (
            "q=.&include=*.{js,md}",
            {"src/util.js", "readme.md"},
            "'{js,md}' brace alternation must match exactly the .js and .md files",
        ),
        (
            "q=.&include=readme.m?",
            {"readme.md"},
            "'?' must match exactly one character (readme.md, nothing else)",
        ),
        (
            # *.ts minus *.test.ts: node_modules/pkg/index.ts stays (no nm exclude).
            "q=.&include=*.ts&exclude=*.test.ts",
            {"app.ts", "src/index.ts", "src/deep/core.ts", "node_modules/pkg/index.ts"},
            "exclude must drop *.test.ts and keep every other included .ts",
        ),
        (
            # **/node_modules prunes that subtree; app.test.ts stays (still .ts).
            "q=.&include=*.ts&exclude=**/node_modules",
            {"app.ts", "app.test.ts", "src/index.ts", "src/deep/core.ts"},
            "'**/node_modules' must prune that subtree and keep all other .ts files",
        ),
        (
            "q=index&include=*.ts",
            {"src/index.ts", "node_modules/pkg/index.ts"},
            "the q substring and the include glob must both apply (AND)",
        ),
    ],
    ids=[
        "include_ext_any_depth",
        "include_subtree",
        "include_braces",
        "include_question_mark",
        "exclude_wins_over_include",
        "exclude_node_modules_subtree",
        "q_and_include_combined",
    ],
)
async def test_search_glob_filters(
    glob_client: httpx.AsyncClient,
    params: str,
    expected: set[str],
    hint: str,
) -> None:
    """GET /search applies include/exclude globs to scope a query's results.

    Exercises the ``_glob_to_regex`` translator end-to-end through the public
    endpoint: extension globs at any depth, subtree globs, brace alternation,
    single-char ``?``, exclude-wins precedence, subtree pruning, and the
    combination of a real text query with an include glob.

    All cases except the last pair the glob with ``q=.``: every file in the
    fixture has an extension, so the dot matches every file and isolates the
    glob filter under test (q is required, so we cannot omit it).

    The result set is asserted for *exact* equality, so a stray match (a file
    the glob shouldn't have kept, or a leaked directory entry) fails the test —
    a subset check would let extras slip through.

    :param glob_client: httpx client backed by the glob workspace.
    :param params: Raw query string for the /search request.
    :param expected: The complete set of paths the search must return.
    :param hint: Explanation surfaced when the assertion fails.
    """
    resp = await glob_client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?{params}"
    )
    assert resp.status_code == 200, resp.text
    paths = {e["path"] for e in resp.json()["data"]}
    # Exact match — proves the glob kept precisely the right files: no missing
    # match and no stray include (wrong file or a leaked directory entry).
    assert paths == expected, f"{hint}: got {paths}, expected {expected}"


@pytest.mark.asyncio
async def test_search_glob_filter_returns_only_files(
    glob_client: httpx.AsyncClient,
) -> None:
    """A glob-scoped search returns file entries only, never directories.

    ``src/**`` matches the ``src/deep`` directory by path, but directories
    must be filtered out so the UI only offers openable files. ``q=.`` matches
    every file (all have extensions) so the include glob is what is exercised.
    """
    resp = await glob_client.get(
        f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/search?q=.&include=src/**"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data, "q=.&include=src/** should match files under src/"
    for entry in data:
        # A directory entry would mean the file-type filter in search_files
        # regressed; the UI cannot open directories.
        assert entry["type"] == "file", (
            f"Expected only file entries, got type={entry['type']!r} for {entry['path']!r}"
        )


# ── Per-session filesystem registry (worktree) ──────────────────────────────


def _git_env() -> dict[str, str]:
    """Build an env dict with dummy git identity.

    :returns: Copy of the current environment with GIT_AUTHOR_* and
        GIT_COMMITTER_* set to safe dummy values.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


@pytest.mark.asyncio
async def test_worktree_session_uses_session_workspace_for_changes(
    tmp_path: Path,
) -> None:
    """The /changes endpoint uses the session's workspace, not the runner's.

    When a session uses a git worktree, its workspace differs from the
    runner's global workspace. The /changes endpoint must run ``git status``
    in the session's workspace to detect changes there, not in the runner's
    startup workspace (where there are no changes).

    Failure means worktree sessions always show "No workspace changes yet"
    even when the agent has modified files in the worktree.
    """
    env = _git_env()

    # Runner's global workspace: a git repo with no uncommitted changes.
    runner_ws = tmp_path / "main-repo"
    runner_ws.mkdir()
    subprocess.run(["git", "init"], cwd=runner_ws, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=runner_ws,
        check=True,
        capture_output=True,
        env=env,
    )

    # Session's workspace: a separate git repo simulating a worktree
    # with an uncommitted change.
    session_ws = tmp_path / "worktree-session"
    session_ws.mkdir()
    subprocess.run(["git", "init"], cwd=session_ws, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=session_ws,
        check=True,
        capture_output=True,
        env=env,
    )
    (session_ws / "agent_change.py").write_text("# written by agent")

    session_id = "conv_worktree_test"

    # Set up the OS environment for the session pointing at the
    # session workspace so _require_os_env passes.
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(session_ws),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    reg = SessionResourceRegistry()
    reg._primary_envs[session_id] = os_env

    # Fake server_client that returns the session's workspace in
    # GET /v1/sessions/{id}.
    session_response = httpx.Response(
        200,
        json={
            "id": session_id,
            "agent_id": "agent_1",
            "status": "idle",
            "created_at": 1000,
            "workspace": str(session_ws),
        },
    )

    async def _mock_transport(request: httpx.Request) -> httpx.Response:
        """Return the session response for GET /v1/sessions/{id}.

        :param request: The outgoing request.
        :returns: Mocked session response.
        """
        if request.url.path == f"/v1/sessions/{session_id}":
            return session_response
        return httpx.Response(404)

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mock_transport),
        base_url="http://fake-server",
    )

    app = create_runner_app(
        resource_registry=reg,
        runner_workspace=runner_ws,
        server_client=server_client,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        resp = await client.get(
            f"/v1/sessions/{session_id}/resources/environments/{DEFAULT_ENVIRONMENT_ID}/changes"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        paths = [e["path"] for e in body["data"]]
        assert "agent_change.py" in paths, (
            f"Expected 'agent_change.py' in changes but got {paths}. "
            "The /changes endpoint is reading from the runner's global "
            "workspace instead of the session's worktree workspace."
        )
