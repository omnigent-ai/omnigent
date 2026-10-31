"""E2E regression: uploaded-bundle cwd validation must reject Windows anchors.

A tenant uploads an agent bundle over multipart ``POST /v1/sessions`` whose
``os_env.cwd`` is a Windows rooted or drive-relative path (``\\Windows``,
``C:``, ``C:Windows``). Those carry a Windows anchor but
``PureWindowsPath.is_absolute()`` is ``False`` for them, so they slipped past
the guard that rejects absolute / ``..``-escaping cwds
(``omnigent/server/bundles.py``: ``_cwd_escapes_workspace``). On a runner
without ``OMNIGENT_RUNNER_WORKSPACE`` such a cwd becomes the agent environment
root / ``copytree`` source, undermining workspace confinement (follow-up to
GHSA-p8rw-8qj3-hf33).

The untrusted-upload guard only runs when the server is multi-user
(``enforce_handler_allowlist=True``). The shared e2e ``live_server`` pins
``OMNIGENT_LOCAL_SINGLE_USER=1`` (``tests/conftest.py``), which disables the
guard entirely, so this module spawns its own header-auth multi-user server
and uploads with an ``X-Forwarded-Email`` tenant identity. POSIX-absolute /
``..`` controls isolate the gap to the Windows anchor; a valid relative cwd
shows ordinary uploads still create a session.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e.conftest import find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TENANT_EMAIL = "tenant@e2e.test"

# Windows rooted / drive-relative paths: they carry an anchor but are not
# reported absolute by PureWindowsPath.is_absolute() -- the reported bypass.
_WINDOWS_ANCHORED_CWDS = (r"\Windows", "C:", r"C:Windows")
# Controls the guard already rejects, isolating the gap to the Windows anchor.
_POSIX_CONTROL_CWDS = ("/etc/passwd", "../outside")
_LOCATIONS = ("root", "terminal", "grandchild", "grandchild_terminal")
_REJECT_MESSAGE = "os_env.cwd must be a relative path within the workspace"


@pytest.fixture(scope="module")
def multiuser_server() -> Iterator[str]:
    """A header-auth multi-user server (untrusted-upload guard enforced).

    Unlike the shared ``live_server`` this drops ``OMNIGENT_LOCAL_SINGLE_USER``
    so ``local_single_user_enabled()`` is False and uploads run the
    untrusted-upload validation. No runner is spawned: bundle validation runs
    before session binding, so rejection is observable without one.
    """
    port = find_free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="omni9131-"))
    env = {**os.environ}
    env.pop("OMNIGENT_LOCAL_SINGLE_USER", None)
    env["OMNIGENT_AUTH_PROVIDER"] = "header"
    apply_server_env(env, _REPO_ROOT)
    log_path = tmpdir / "server.log"
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [
                server_executable(),
                "-m",
                "omnigent.cli",
                "server",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{tmpdir / 'server.db'}",
                "--artifact-location",
                str(tmpdir / "artifacts"),
            ],
            env=env,
            cwd=compat_server_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if proc.poll() is not None:
                    raise RuntimeError(f"server exited early:\n{log_path.read_text()[-2000:]}")
                time.sleep(0.1)
            else:
                raise RuntimeError(f"server did not start:\n{log_path.read_text()[-2000:]}")
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(scope="module")
def tenant_client(multiuser_server: str) -> Iterator[httpx.Client]:
    """Client authenticated as an untrusted tenant via header identity."""
    with httpx.Client(
        base_url=multiuser_server,
        timeout=60,
        headers={"X-Forwarded-Email": _TENANT_EMAIL, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    ) as client:
        yield client


def _os_env(cwd: str) -> dict[str, object]:
    return {"type": "caller_process", "cwd": cwd, "sandbox": {"type": "none"}}


def _config(name: str) -> dict[str, object]:
    return {
        "spec_version": 1,
        "name": name,
        "executor": {
            "type": "omnigent",
            "model": "gpt-5.4",
            "config": {"harness": "openai-agents"},
        },
        "instructions": "hello",
    }


def _bundle_with_cwd(location: str, cwd: str) -> bytes:
    """Build a ``.tar.gz`` bundle pinning ``cwd`` at the given location.

    ``location`` mirrors the reported surfaces: the root agent's os_env, a
    root terminal, a nested (grandchild) agent, and a nested agent's terminal.
    """
    root = _config("root")
    files: dict[str, dict[str, object]] = {"config.yaml": root}

    if location == "root":
        root["os_env"] = _os_env(cwd)
    elif location == "terminal":
        root["terminals"] = {"shell": {"command": "sh", "os_env": _os_env(cwd)}}
    else:
        child = _config("child")
        grandchild = _config("grandchild")
        root["tools"] = {"agents": ["child"]}
        child["tools"] = {"agents": ["grandchild"]}
        if location == "grandchild":
            grandchild["os_env"] = _os_env(cwd)
        else:  # grandchild_terminal
            grandchild["terminals"] = {"shell": {"command": "sh", "os_env": _os_env(cwd)}}
        files["agents/child/config.yaml"] = child
        files["agents/child/agents/grandchild/config.yaml"] = grandchild

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, config in files.items():
            content = json.dumps(config).encode()
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _post_bundle(client: httpx.Client, bundle: bytes) -> httpx.Response:
    return client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )


@pytest.mark.parametrize("location", _LOCATIONS)
@pytest.mark.parametrize("bad_cwd", _WINDOWS_ANCHORED_CWDS)
def test_upload_rejects_windows_anchored_cwd(
    tenant_client: httpx.Client,
    location: str,
    bad_cwd: str,
) -> None:
    """An untrusted upload with a Windows-anchored cwd must be rejected (400)."""
    resp = _post_bundle(tenant_client, _bundle_with_cwd(location, bad_cwd))
    assert resp.status_code == 400, (
        f"Windows-anchored cwd {bad_cwd!r} at {location} was accepted "
        f"(status {resp.status_code}); expected 400 rejection. Body: {resp.text}"
    )
    assert _REJECT_MESSAGE in resp.text


@pytest.mark.parametrize("location", _LOCATIONS)
@pytest.mark.parametrize("bad_cwd", _POSIX_CONTROL_CWDS)
def test_upload_rejects_posix_escaping_cwd(
    tenant_client: httpx.Client,
    location: str,
    bad_cwd: str,
) -> None:
    """Control: POSIX-absolute / ``..`` anchors are already rejected."""
    resp = _post_bundle(tenant_client, _bundle_with_cwd(location, bad_cwd))
    assert resp.status_code == 400, resp.text
    assert _REJECT_MESSAGE in resp.text


def test_upload_accepts_relative_cwd(tenant_client: httpx.Client) -> None:
    """Control: an ordinary workspace-relative cwd still creates a session."""
    resp = _post_bundle(tenant_client, _bundle_with_cwd("root", "subdir"))
    resp.raise_for_status()
    assert resp.json()["session_id"]
