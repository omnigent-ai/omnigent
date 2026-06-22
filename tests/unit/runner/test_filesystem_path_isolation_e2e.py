"""Negative tests for workspace filesystem path isolation and sensitive files.

Drives the full runner HTTP filesystem stack (``CallerProcessFilesystem`` →
``OSEnvironment`` helper → ``_assert_within_cwd``). The inner guard is
unit-tested in ``test_runner_filesystem_hardening.py``; here we assert the
HTTP endpoints block traversal, absolute paths, and symlink escapes, and never
leak or mutate out-of-root content.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests.unit.runner.helpers import NullServerClient

_SECRET = "TOP-SECRET-CREDENTIAL-do-not-leak"
_BASE = f"/v1/sessions/conv_iso/resources/environments/{DEFAULT_ENVIRONMENT_ID}"


@pytest.fixture
def planted(tmp_path: Path) -> Path:
    """Workspace with symlinks escaping to out-of-root secrets.

    Layout::

        tmp/
          workspace/            <- environment root
            hello.txt
            escape.txt          -> ../outside_secret.txt
            vendor/             -> ../personal
          outside_secret.txt    (_SECRET)
          personal/id_rsa       (_SECRET)
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.txt").write_text("hello world")

    secret_file = tmp_path / "outside_secret.txt"
    secret_file.write_text(_SECRET)
    (ws / "escape.txt").symlink_to(secret_file)

    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "id_rsa").write_text(_SECRET)
    (ws / "vendor").symlink_to(personal)

    return tmp_path


@pytest.fixture
def app(planted: Path) -> FastAPI:
    """Runner app rooted at the planted workspace.

    ``sandbox=none`` is deliberate: it proves isolation holds without any
    OS-level sandbox backend.
    """
    workspace = planted / "workspace"
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )
    assert os_env is not None
    reg = SessionResourceRegistry()
    reg._primary_envs["conv_iso"] = os_env
    return create_runner_app(
        resource_registry=reg,
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client bound to the runner app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,json_body",
    [
        ("PUT", {"content": "x", "encoding": "utf-8"}),
        ("PATCH", {"old_text": "a", "new_text": "b"}),
        ("DELETE", None),
    ],
    ids=["write", "edit", "delete"],
)
async def test_traversal_rejected_on_mutating_methods(
    client: httpx.AsyncClient,
    method: str,
    json_body: dict | None,
) -> None:
    """Encoded ``../`` traversal is rejected on write/edit/delete (not just GET)."""
    resp = await client.request(
        method,
        f"{_BASE}/filesystem/" + "..%2F..%2Fetc%2Fpasswd",
        json=json_body,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,json_body",
    [
        ("GET", None),
        ("PUT", {"content": "x", "encoding": "utf-8"}),
        ("DELETE", None),
    ],
    ids=["read", "write", "delete"],
)
async def test_absolute_path_rejected(
    client: httpx.AsyncClient,
    method: str,
    json_body: dict | None,
) -> None:
    """An absolute path (encoded leading slash) is rejected on read/write/delete."""
    resp = await client.request(
        method,
        f"{_BASE}/filesystem/" + "%2Fetc%2Fpasswd",
        json=json_body,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_symlink_read_escape_blocked_via_http(
    client: httpx.AsyncClient,
) -> None:
    """Reading an in-workspace symlink to an out-of-root file leaks nothing."""
    # ``escape.txt`` passes string validation (plain relative name); only the
    # resolved-path guard stops the read.
    resp = await client.get(f"{_BASE}/filesystem/escape.txt")
    assert resp.status_code != 200, resp.text
    assert _SECRET not in resp.text


@pytest.mark.asyncio
async def test_symlink_write_escape_blocked_via_http(
    client: httpx.AsyncClient,
    planted: Path,
) -> None:
    """Writing through an in-workspace symlink must not mutate the out-of-root file."""
    outside = planted / "outside_secret.txt"
    resp = await client.put(
        f"{_BASE}/filesystem/escape.txt",
        json={"content": "OVERWRITTEN-BY-ATTACKER", "encoding": "utf-8"},
    )
    assert resp.status_code != 200, resp.text
    # Decisive check: the out-of-root file is unchanged.
    assert outside.read_text() == _SECRET


@pytest.mark.asyncio
async def test_read_through_symlinked_directory_blocked(
    client: httpx.AsyncClient,
) -> None:
    """A read into a symlinked out-of-root directory is blocked."""
    # ``vendor`` -> out-of-root ``personal/``; ``vendor/id_rsa`` has no ``..`` so
    # string validation passes and only the resolved-path guard can refuse it.
    resp = await client.get(f"{_BASE}/filesystem/vendor/id_rsa")
    assert resp.status_code != 200, resp.text
    assert _SECRET not in resp.text


@pytest.mark.asyncio
async def test_in_workspace_read_still_works(
    client: httpx.AsyncClient,
) -> None:
    """Control: a legitimate in-root read succeeds (negatives aren't vacuous)."""
    resp = await client.get(f"{_BASE}/filesystem/hello.txt")
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello world"


@pytest.mark.asyncio
async def test_shell_in_workspace_read_works(
    client: httpx.AsyncClient,
) -> None:
    """Control: an in-root shell read succeeds, proving /shell is wired.

    Without this, the out-of-root xfails below could pass vacuously (a dead
    endpoint returns empty stdout, so ``_SECRET not in stdout`` holds).
    """
    resp = await client.post(
        f"{_BASE}/shell",
        json={"command": "cat hello.txt"},
    )
    assert resp.status_code == 200, resp.text
    assert "hello world" in resp.json().get("stdout", "")


# The filesystem API blocks every vector below; /shell does not, because it is
# confined only by an active OS sandbox backend and here sandbox=none. Each case
# is a distinct escape a sharing-safety gate must close. They are
# pinned strict so the whole matrix must flip together when the gate lands.
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Known gap: /shell with sandbox=none is not confined to workspace root.",
)
@pytest.mark.parametrize(
    "command",
    [
        "cat ../outside_secret.txt",  # relative traversal
        "cat escape.txt",  # symlink -> out-of-root file
        "cat vendor/id_rsa",  # symlink -> out-of-root dir (creds shape)
    ],
    ids=["traversal", "symlink-file", "symlink-dir"],
)
async def test_shell_cannot_read_outside_workspace(
    client: httpx.AsyncClient,
    command: str,
) -> None:
    """A shared session's shell must not read out-of-root or sensitive files.

    Fails (xpass) once a sharing-safety gate confines /shell to the workspace
    root the way the filesystem API already does; today the secret leaks.
    """
    resp = await client.post(f"{_BASE}/shell", json={"command": command})
    # Decisive: the secret must never reach stdout regardless of the vector.
    assert _SECRET not in resp.json().get("stdout", "")


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Known gap: /shell with sandbox=none can write outside workspace root.",
)
async def test_shell_cannot_write_outside_workspace(
    client: httpx.AsyncClient,
    planted: Path,
) -> None:
    """A shared session's shell must not mutate out-of-root files."""
    outside = planted / "outside_secret.txt"
    await client.post(
        f"{_BASE}/shell",
        json={"command": "echo OVERWRITTEN > ../outside_secret.txt"},
    )
    # Decisive: the out-of-root file is unchanged.
    assert outside.read_text() == _SECRET
