"""E2E test -- Session Resources API against ``omnigent server`` (mock LLM).

Boots ``omnigent server --agent <yaml>`` as a subprocess and
exercises every session-resource endpoint. No LLM calls are made --
the test only needs the server to boot and register the agent.

Migrated to mock LLM: uses ``mock_credentials_env`` so the server
subprocess doesn't depend on real Databricks credentials in the
environment.

Design reference: ``designs/SESSION_RESOURCES_API_DESIGN.md``
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import socket
import subprocess
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

_BOOT_TIMEOUT = 30.0
_API_TIMEOUT = 15.0

_AGENT_YAML = """\
spec_version: 1
name: e2e_resources_test
prompt: Test agent for session resources e2e.
executor:
  model: databricks-gpt-5-4-mini
  config:
    harness: openai-agents
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _find_free_port() -> int:
    """Return a free TCP port on localhost.

    :returns: An ephemeral port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict[str, str]:
    """Build a subprocess env with stale credential vars removed.

    Sets PYTHONPATH to the worktree root so the subprocess
    imports this checkout's code (essential for git worktrees).

    :returns: Copy of os.environ without vars that would confuse
        the server or cause it to attempt LLM auth.
    """
    env = dict(os.environ)
    for var in (
        "ANTHROPIC_API_KEY",
        "DATABRICKS_TOKEN",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CODEX",
    ):
        env.pop(var, None)
    repo = str(Path(__file__).resolve().parents[3])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo, existing) if p)
    return env


@contextmanager
def _omnigent_server(
    *,
    python: Path,
    agent_path: Path,
    port: int,
    env: dict[str, str],
    cwd: Path,
    db_path: Path,
    runner_id: str,
    binding_token: str,
) -> Iterator[subprocess.Popen[str]]:
    """Spawn ``omnigent server --agent <agent_path>`` and a sibling runner.

    :param python: Interpreter with omnigent installed.
    :param agent_path: Absolute path to the agent directory.
    :param port: Bind port.
    :param env: Subprocess environment (without runner-specific vars).
    :param cwd: Working directory.
    :param db_path: Path for the SQLite database.
    :param runner_id: Runner id derived from *binding_token*.
    :param binding_token: Tunnel binding token shared between server
        and runner.
    :yields: The live subprocess.
    """
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            str(python),
            "-m",
            "omnigent",
            "server",
            "--agent",
            str(agent_path),
            "-p",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
        ],
        env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Spawn runner as sibling subprocess.
    runner_proc = subprocess.Popen(
        [str(python), "-m", "omnigent.runner._entry"],
        env={
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        },
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        yield proc
    finally:
        if runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _wait_for_health(
    port: int,
    *,
    timeout: float,
    proc: subprocess.Popen[str],
    runner_id: str | None = None,
) -> None:
    """Poll /health until the server responds 200.

    :param port: Bound port.
    :param timeout: Max seconds to wait.
    :param proc: The server subprocess.
    :param runner_id: Optional runner id to require online before
        returning, e.g. ``"runner_e2e"``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"Server exited with code {proc.returncode}:\n{stdout}")
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{port}/health",
                timeout=2.0,
            )
            if resp.status_code == 200 and runner_id is None:
                return
            if resp.status_code == 200 and runner_id is not None:
                runner_resp = httpx.get(
                    f"http://127.0.0.1:{port}/v1/runners/{runner_id}/status",
                    timeout=2.0,
                )
                if runner_resp.status_code == 200 and runner_resp.json()["online"] is True:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    pytest.fail(f"Server not ready after {timeout}s")


def _bundle_yaml(yaml_path: Path) -> bytes:
    """
    Package a single YAML spec as an agent tarball.

    :param yaml_path: YAML file to place at ``config.yaml``.
    :returns: Gzipped tar archive bytes accepted by
        ``POST /v1/sessions``.
    """
    config_bytes = yaml_path.read_bytes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        archive.addfile(info, io.BytesIO(config_bytes))
    return buffer.getvalue()


def _create_session(client: httpx.Client, yaml_path: Path, runner_id: str) -> str:
    """Create a session and return the session_id.

    :param client: httpx client pointed at the server.
    :param yaml_path: Agent YAML file to upload as the session bundle.
    :param runner_id: Online runner id to bind, e.g.
        ``"runner_e2e"``.
    :returns: The session id.
    """
    session_resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _bundle_yaml(yaml_path),
                "application/gzip",
            )
        },
    )
    assert session_resp.status_code == 201, session_resp.text
    session_id = session_resp.json()["session_id"]
    bind_resp = client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
    )
    bind_resp.raise_for_status()
    return session_id


def _resolve_python() -> Path:
    """Find the .venv python, walking up from this file.

    :returns: Path to the venv Python interpreter.
    """
    current = Path(__file__).resolve().parents[3]
    while True:
        candidate = current / ".venv" / "bin" / "python"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            pytest.fail("No .venv/bin/python found")
        current = current.parent


# ── Test ─────────────────────────────────────────────────────────


def test_session_resources_e2e(
    mock_credentials_env: dict[str, str],
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """Full session resources API round-trip against a real server.

    No LLM credentials needed -- the test only exercises the
    resource API surface, not the agent execution path. Uses
    ``mock_credentials_env`` so no real credentials leak through.

    :param mock_credentials_env: Mock credentials env from conftest.
    :param omnigent_python: Python interpreter fixture.
    :param omnigent_repo_root: Repo root fixture.
    :param tmp_path: Pytest temp directory for the agent YAML
        and SQLite database.
    """
    from omnigent.runner.identity import token_bound_runner_id

    python = omnigent_python
    repo_root = omnigent_repo_root
    port = _find_free_port()

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    yaml_path = agent_dir / "config.yaml"
    yaml_path.write_text(_AGENT_YAML)
    db_path = tmp_path / "test.db"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = dict(mock_credentials_env)

    with _omnigent_server(
        python=python,
        agent_path=agent_dir,
        port=port,
        env=env,
        cwd=repo_root,
        db_path=db_path,
        runner_id=runner_id,
        binding_token=binding_token,
    ) as proc:
        _wait_for_health(port, timeout=_BOOT_TIMEOUT, proc=proc, runner_id=runner_id)

        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=_API_TIMEOUT,
        ) as client:
            session_id = _create_session(client, yaml_path, runner_id)

            # ── Unified inventory ────────────────────────────
            resp = client.get(
                f"/v1/sessions/{session_id}/resources?order=asc",
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["object"] == "list"
            ids = [r["id"] for r in body["data"]]
            assert "default" in ids

            # ── Typed environment collection ──────────────────
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/environments",
            )
            assert resp.status_code == 200
            types = {r["type"] for r in resp.json()["data"]}
            assert types <= {"environment"}

            # ── Single environment lookup ─────────────────────
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/environments/default",
            )
            assert resp.status_code == 200
            assert resp.json()["id"] == "default"

            # ── Terminals (auto-created REPL terminal) ────────
            # Runner-hosted SDK sessions auto-create the embedded
            # Omnigent REPL terminal (``terminal_tui_main``) on bind;
            # it is the only terminal until the agent launches more.
            # An empty list means the auto-create regressed; extra
            # entries mean something else launched unexpectedly.
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/terminals",
            )
            assert resp.status_code == 200
            assert [t["id"] for t in resp.json()["data"]] == ["terminal_tui_main"]

            # ── Type filter ───────────────────────────────────
            resp = client.get(
                f"/v1/sessions/{session_id}/resources?type=environment",
            )
            assert resp.status_code == 200
            for r in resp.json()["data"]:
                assert r["type"] == "environment"

            # ── 404 for unknown session ───────────────────────
            resp = client.get(
                "/v1/sessions/conv_nonexistent/resources",
            )
            assert resp.status_code == 404

            # ── File lifecycle ────────────────────────────────
            content = f"e2e test {time.time()}"
            resp = client.post(
                f"/v1/sessions/{session_id}/resources/files",
                files={
                    "file": (
                        "e2e_test.txt",
                        content.encode(),
                        "text/plain",
                    ),
                },
            )
            assert resp.status_code == 201
            file_id = resp.json()["id"]
            assert resp.json()["type"] == "file"

            # File in list
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/files",
            )
            assert file_id in [f["id"] for f in resp.json()["data"]]

            # File in unified inventory
            resp = client.get(
                f"/v1/sessions/{session_id}/resources?order=asc",
            )
            assert file_id in [r["id"] for r in resp.json()["data"]]

            # Download content
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}/content",
            )
            assert resp.status_code == 200
            assert resp.text == content

            # Ownership: wrong session → 404
            resp = client.get(
                f"/v1/sessions/conv_nonexistent/resources/files/{file_id}",
            )
            assert resp.status_code == 404

            # Delete file
            resp = client.delete(
                f"/v1/sessions/{session_id}/resources/files/{file_id}",
            )
            assert resp.status_code == 200
            assert resp.json()["deleted"] is True

            # Confirm gone
            resp = client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}",
            )
            assert resp.status_code == 404

            # ── Filesystem operations ─────────────────────────
            fs = f"/v1/sessions/{session_id}/resources/environments/default/filesystem"

            # List root
            resp = client.get(fs)
            assert resp.status_code == 200, resp.text
            assert resp.json()["object"] == "list"

            # Write
            resp = client.put(
                f"{fs}/_e2e_test.txt",
                json={
                    "content": "hello e2e",
                    "encoding": "utf-8",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["created"] is True

            # Read
            resp = client.get(f"{fs}/_e2e_test.txt")
            assert resp.status_code == 200
            assert resp.json()["content"] == "hello e2e"

            # Edit
            resp = client.patch(
                f"{fs}/_e2e_test.txt",
                json={
                    "old_text": "hello",
                    "new_text": "goodbye",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["replacements"] == 1

            # Verify edit
            resp = client.get(f"{fs}/_e2e_test.txt")
            assert resp.json()["content"] == "goodbye e2e"

            # Delete
            resp = client.delete(f"{fs}/_e2e_test.txt")
            assert resp.status_code == 200
            assert resp.json()["deleted"] is True

            # Confirm gone
            resp = client.get(f"{fs}/_e2e_test.txt")
            assert resp.status_code == 404

            # ── Shell execution ───────────────────────────────
            shell_url = f"/v1/sessions/{session_id}/resources/environments/default/shell"

            resp = client.post(
                shell_url,
                json={"command": "echo e2e_shell_test"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["stdout"].strip() == "e2e_shell_test"
            assert body["exit_code"] == 0
            assert body["timed_out"] is False
