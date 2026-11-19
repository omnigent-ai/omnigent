"""Who may call each workspace API method on the host's socket and tunnel.

Every refusal must come before any search or Git process starts, so each case
runs with a guard that fails the test if one does. The server route's own
gates (owner, confinement, the content-search flag) are in
``test_workspace_service.py`` and ``tests/server/routes/test_shell_permission_gate.py``.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.host.frames import HostWorkspaceCallFrame
from omnigent.host.workspace_service import serve_tunnel_call, start_workspace_socket
from omnigent.host.workspace_tokens import mint_token
from omnigent.workspace_fs import WorkspaceReader

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="needs Unix sockets"
)

_SERVICE = "/omnigent.api.workspace.v1alpha1.WorkspaceService"
# Each new method with a request that would start work if it were let through.
_CALLS: dict[str, dict[str, object]] = {
    "StatPath": {"path": "a.txt"},
    "ReadLinkText": {"path": "link"},
    "SearchFiles": {"query": "a", "mode": "FILE_SEARCH_MODE_SUBSTRING"},
    "SearchText": {"query": "needle"},
    "ResolveWorkingCopyBase": {},
    "ListWorkingCopyChanges": {},
    "ReadBaseFile": {"commit": "a" * 40, "path": "a.txt", "object": "b" * 40},
    "ListRepositoryRefs": {},
    "ListBranchCommits": {},
    "ResolveRepositoryContext": {},
}
_STREAMING = {"SearchText", "ReadBaseFile"}


def _workspace(root: Path, marker: str) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text(f"needle {marker}\n")
    (root / "link").symlink_to("a.txt")
    return root


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Record every child process started; asyncio starts its children through Popen too."""
    started: list[tuple[str, ...]] = []
    real = subprocess.Popen

    def popen(argv: list[str], *args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        started.append(tuple(str(a) for a in argv))
        return real(argv, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "Popen", popen)
    yield started


@pytest.fixture
async def socket_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, str, Path, Path]]:
    """A client of a real host socket, its key, and workspaces A and B."""
    workspace_a = _workspace(tmp_path / "a", "from-a")
    workspace_b = _workspace(tmp_path / "b", "from-b")
    listener = await start_workspace_socket(lambda ws: WorkspaceReader(Path(ws)), host_id="host_a")
    assert listener is not None
    transport = httpx.AsyncHTTPTransport(uds=str(listener.path))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://workspace") as http:
            yield http, listener.token_key, workspace_a, workspace_b
    finally:
        await listener.stop()


def _bearer(
    key: str, workspace: Path, *, host_id: str = "host_a", now: float | None = None
) -> str:
    token, _ = mint_token(key, host_id=host_id, session_id="s1", workspace=str(workspace), now=now)
    return f"Bearer {token}"


async def _post(
    http: httpx.AsyncClient,
    method: str,
    body: dict[str, object],
    headers: dict[str, str],
    *,
    path: str | None = None,
) -> httpx.Response:
    if method in _STREAMING:
        import json

        payload = json.dumps(body).encode()
        content = b"\x00" + len(payload).to_bytes(4, "big") + payload
        headers = {**headers, "Content-Type": "application/connect+json"}
    else:
        content = None
    return await http.post(
        path or f"{_SERVICE}/{method}",
        json=None if content is not None else body,
        content=content,
        headers=headers,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(_CALLS))
@pytest.mark.parametrize(
    "credential", ["missing", "not-a-token", "expired", "another-host", "another-key"]
)
async def test_socket_refuses_bad_tokens_before_any_work(
    socket_client: tuple[httpx.AsyncClient, str, Path, Path],
    no_processes: list[tuple[str, ...]],
    method: str,
    credential: str,
) -> None:
    http, key, workspace_a, _ = socket_client
    authorization = {
        "missing": None,
        "not-a-token": "Bearer not-a-token",
        "expired": _bearer(key, workspace_a, now=time.time() - 3600),
        "another-host": _bearer(key, workspace_a, host_id="host_b"),
        "another-key": _bearer("another-host-process-key-0123456789", workspace_a),
    }[credential]

    response = await _post(
        http, method, _CALLS[method], {"Authorization": authorization} if authorization else {}
    )

    if method in _STREAMING:
        assert b'"unauthenticated"' in response.content, response.text
    else:
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "unauthenticated"
    assert no_processes == []


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="searches with ripgrep")
async def test_a_grant_for_one_workspace_stays_in_it_whatever_the_request_says(
    socket_client: tuple[httpx.AsyncClient, str, Path, Path],
) -> None:
    """Headers, paths and scopes can't move a call to another workspace."""
    http, key, workspace_a, workspace_b = socket_client
    headers = {
        "Authorization": _bearer(key, workspace_a),
        "omnigent-workspace-id": str(workspace_b),
        "X-Omnigent-Workspace": str(workspace_b),
    }

    stat = await _post(http, "StatPath", {"path": ""}, headers)
    searched = await _post(http, "SearchText", {"query": "needle"}, headers)
    files = await _post(
        http, "SearchFiles", {"query": "a", "mode": "FILE_SEARCH_MODE_SUBSTRING"}, headers
    )
    outward = await _post(
        http,
        "SearchText",
        {"query": "needle", "include_directories": [f"../{workspace_b.name}"]},
        headers,
    )

    assert stat.json()["entry"]["name"] == "a"
    assert b"from-a" in searched.content and b"from-b" not in searched.content
    assert [r["entry"]["path"] for r in files.json()["results"]] == ["a.txt"]
    assert b"invalid_argument" in outward.content and b"from-b" not in outward.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["Bogus", "GetInfo", "OpenWorkspace", "CloseWorkspace", "list_directory"]
)
async def test_socket_refuses_methods_outside_the_service(
    socket_client: tuple[httpx.AsyncClient, str, Path, Path],
    no_processes: list[tuple[str, ...]],
    method: str,
) -> None:
    http, key, workspace_a, _ = socket_client
    headers = {"Authorization": _bearer(key, workspace_a)}

    response = await http.post(f"{_SERVICE}/{method}", json={}, headers=headers)
    helper = await http.post(
        f"/omnigent.api.workspace.v1alpha1.WorkspaceHelperService/{method}",
        json={},
        headers=headers,
    )

    assert response.status_code == helper.status_code == 404
    assert no_processes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["Bogus", "GetInfo", "OpenWorkspace", "CloseWorkspace", "search_files"]
)
async def test_tunnel_refuses_methods_outside_the_table_before_any_work(
    tmp_path: Path, no_processes: list[tuple[str, ...]], method: str
) -> None:
    frame = HostWorkspaceCallFrame(
        request_id="r1", workspace=str(_workspace(tmp_path / "w", "w")), method=method
    )
    no_processes.clear()  # the workspace's own setup

    replies = [
        reply async for reply in serve_tunnel_call(lambda ws: WorkspaceReader(Path(ws)), frame)
    ]

    assert [(r.done, r.error_code) for r in replies] == [(True, "unimplemented")]
    assert no_processes == []
