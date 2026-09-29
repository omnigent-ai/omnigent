"""A drained sub-agent cancellation is corrected by the child's confirmed late completion.

Real server, runner and Claude CLI with a mocked model API; run with ``pytest -o addopts=''``."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests.e2e.conftest import find_free_port

pytestmark = pytest.mark.timeout(360, method="signal")
_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-subagent-status-parent"
_CHILD_MODEL = "claude-sonnet-4-20250514"
_VERDICT = "VERDICT_all_three_citations_check_out"


@pytest.fixture
def rig(
    isolated_mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[httpx.Client, str, str]]:
    """Run an isolated server/runner; the child uses the real Claude CLI + mock API."""
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} binary")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip("machine-managed Claude settings override mock auth; run in a clean container")
    mock_url = isolated_mock_llm_server_url
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    native_home = tmp_path / "home"
    claude_home = native_home / ".claude"
    for directory in (workspace, config_dir, claude_home):
        directory.mkdir(parents=True)
    (native_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace.resolve()): {"hasTrustDialogAccepted": True}},
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "repro-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _CHILD_MODEL},
                        },
                    },
                    "repro-openai": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    token = uuid.uuid4().hex
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "SYSTEMROOT",
                "WINDIR",
                "TMPDIR",
                "TMP",
                "TEMP",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
                "NODE_EXTRA_CA_CERTS",
            }
        },
        "HOME": str(native_home),
        "OMNIGENT_CONFIG_HOME": str(config_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "OMNIGENT_CLAUDE_PATH": str(shutil.which("claude")),
        "PYTHONPATH": str(_REPO),
    }
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = "127.0.0.1,localhost"
    processes: list[subprocess.Popen[bytes]] = []
    with (
        (tmp_path / "server.log").open("w") as server_log,
        (tmp_path / "runner.log").open("w") as runner_log,
        httpx.Client(
            base_url=base_url,
            timeout=30,
            trust_env=False,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
        ) as client,
    ):
        try:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "omnigent.cli",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{tmp_path / 'test.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    cwd=_REPO,
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token},
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    cwd=_REPO,
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert all(proc.poll() is None for proc in processes), "server/runner exited"
                with contextlib.suppress(httpx.HTTPError):
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    if response.status_code == 200 and response.json().get("online"):
                        break
                time.sleep(0.5)
            else:
                pytest.fail("server/runner did not become ready")
            yield client, runner_id, mock_url
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


def _configure_mock(mock_url: str) -> None:
    # Parent: dispatch the child, then drain its inbox each time it is woken.
    httpx.post(
        f"{mock_url}/mock/configure",
        json={
            "key": _PARENT_MODEL,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_dispatch",
                            "name": "sys_session_send",
                            "arguments": json.dumps(
                                {
                                    "agent": "researcher",
                                    "title": "cite-check",
                                    "args": "Verify the citations and report a verdict.",
                                }
                            ),
                        }
                    ]
                },
                {"text": "Researcher dispatched; waiting for its result."},
                {
                    "tool_calls": [
                        {"call_id": "drain1", "name": "sys_read_inbox", "arguments": "{}"}
                    ]
                },
                {"text": "Inbox drained (1)."},
                {
                    "tool_calls": [
                        {"call_id": "drain2", "name": "sys_read_inbox", "arguments": "{}"}
                    ]
                },
                {"text": "Inbox drained (2)."},
                {
                    "tool_calls": [
                        {"call_id": "drain3", "name": "sys_read_inbox", "arguments": "{}"}
                    ]
                },
                {"text": "Inbox drained (3)."},
            ],
        },
        timeout=10,
    ).raise_for_status()
    # Every child model request parks on the gate so its turn cannot finish (nor
    # post its own idle edge) until the test releases it — the outcome is unknown.
    httpx.post(
        f"{mock_url}/mock/configure",
        json={"key": _CHILD_MODEL, "responses": [{"text": _VERDICT, "block": True}] * 12},
        timeout=10,
    ).raise_for_status()
    for key, text in (
        (_CHILD_MODEL, _VERDICT),
        (_PARENT_MODEL, "Acknowledged."),
        ("default", "ok"),
    ):
        httpx.post(
            f"{mock_url}/mock/set_fallback", json={"key": key, "text": text}, timeout=10
        ).raise_for_status()


def _register_parent(client: httpx.Client, mock_url: str) -> str:
    name = f"subagent-status-parent-{uuid.uuid4().hex[:8]}"
    spec = {
        "name": name,
        "prompt": (
            "You are an orchestrator. Dispatch the researcher sub-agent via "
            "sys_session_send when asked, and read your inbox when woken."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": _PARENT_MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_url}/v1"},
        },
        "tools": {
            "researcher": {
                "type": "agent",
                "description": "Claude Code researcher sub-agent.",
                "executor": {"harness": "claude-native"},
                "prompt": "You are a citation-checking researcher.",
            }
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(spec).encode()
        info = tarfile.TarInfo(f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
    )
    assert resp.status_code in (200, 201, 409), f"{resp.status_code} {resp.text[:400]}"
    listing = client.get(
        "/v1/sessions", params={"visibility": "all", "agent_name": name, "limit": 1}
    )
    listing.raise_for_status()
    return str(listing.json()["data"][0]["agent_id"])


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    resp = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _inbox_deliveries(client: httpx.Client, parent_id: str) -> list[str]:
    """Return the text of every sub-agent inbox payload the parent has drained."""
    call_ids: set[str] = set()
    out: list[str] = []
    for item in _items(client, parent_id):
        data = item.get("data") or {}
        typ = item.get("type") or data.get("type")
        name = item.get("name") or data.get("name")
        cid = item.get("call_id") or data.get("call_id")
        if typ == "function_call" and name == "sys_read_inbox":
            call_ids.add(cid)
        if typ == "function_call_output" and cid in call_ids:
            text = item.get("output") or data.get("output")
            if text:
                out.append(text)
    return out


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _wait_until(predicate, *, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _dispatch_parked_child(client: httpx.Client, runner_id: str, mock_url: str) -> tuple[str, str]:
    """Dispatch a real claude-native child and hold its turn mid-flight on the gate."""
    _configure_mock(mock_url)
    agent_id = _register_parent(client, mock_url)
    create = client.post("/v1/sessions", json={"agent_id": agent_id})
    create.raise_for_status()
    parent_id = str(create.json()["id"])
    client.patch(f"/v1/sessions/{parent_id}", json={"runner_id": runner_id}).raise_for_status()

    send = client.post(
        f"/v1/sessions/{parent_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Dispatch the researcher sub-agent."}],
            },
        },
    )
    assert send.status_code == 202, f"{send.status_code} {send.text}"

    child_id: str | None = None

    def child_seen() -> bool:
        nonlocal child_id
        resp = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        if resp.status_code == 200 and resp.json().get("data"):
            row = resp.json()["data"][0]
            child_id = str(row.get("session_id") or row.get("id"))
            return True
        return False

    assert _wait_until(child_seen, timeout=120), "parent never dispatched the child"
    assert child_id is not None
    assert _wait_until(lambda: _gate_pending(mock_url), timeout=90), "child turn never parked"
    # Settle so this child's blocked request is the pending gate and its work
    # entry is tracked before we trigger; the parent has not been woken yet.
    time.sleep(3)
    assert _gate_pending(mock_url)
    assert _inbox_deliveries(client, parent_id) == []
    return parent_id, child_id


def _stop(client: httpx.Client, *session_ids: str) -> None:
    for sid in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            client.post(f"/v1/sessions/{sid}/events", json={"type": "stop_session"}, timeout=5)


def test_drained_cancellation_corrected_by_surviving_completion(
    rig: tuple[httpx.Client, str, str],
) -> None:
    """A surviving child's result must reach the parent even after the cancellation drained."""
    client, runner_id, mock_url = rig
    parent_id, child_id = _dispatch_parked_child(client, runner_id, mock_url)
    try:
        # The web Stop button posts this exact event while the child turn is
        # still in flight (parked on the gate).
        resp = client.post(f"/v1/sessions/{child_id}/events", json={"type": "interrupt"})
        assert resp.status_code in (202, 204), f"{resp.status_code} {resp.text}"

        # Drain-first is the durability-critical step: once the cancellation is
        # drained the work entry is gone, so a later completion cannot correct it.
        drained = _wait_until(
            lambda: any("cancelled" in d for d in _inbox_deliveries(client, parent_id)),
            timeout=60,
        )
        assert drained, (
            "parent never drained the optimistic cancellation; "
            f"inbox: {_inbox_deliveries(client, parent_id)}"
        )
        # The turn is still unfinished (parked): the child genuinely survived
        # the interrupt rather than being confirmed stopped.
        time.sleep(2)
        assert _gate_pending(mock_url), "child turn should still be in flight after Stop"

        # The child survives the Escape and finishes: its forwarder posts the
        # genuine turn-end completion (Claude's Stop hook, stamped
        # ``turn_completed``) carrying the real verdict.
        resp = client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": _VERDICT, "turn_completed": True},
            },
        )
        assert resp.status_code in (202, 204), f"{resp.status_code} {resp.text}"
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_url}/gate/release", timeout=5)

        delivered = _wait_until(
            lambda: any(_VERDICT in d for d in _inbox_deliveries(client, parent_id)),
            timeout=45,
            interval=2.0,
        )
        assert delivered, (
            "surviving sub-agent's genuine result was discarded after the "
            "cancellation was drained; "
            f"parent inbox: {_inbox_deliveries(client, parent_id)}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_url}/gate/release", timeout=5)
        _stop(client, child_id, parent_id)
