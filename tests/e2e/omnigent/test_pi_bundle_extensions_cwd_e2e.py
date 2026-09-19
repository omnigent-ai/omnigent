"""E2E: a ``harness: pi`` agent bundle's project-local ``.pi/extensions``
and bundle-root ``AGENTS.md`` must reach the session's actual pi process.

Pi auto-discovers project-local extensions (``.pi/extensions/<name>/``) and
context files (``AGENTS.md``) from its **process cwd**. The runner spawns pi
with ``cwd`` set to the session workspace, which is unrelated to the agent
bundle's on-disk location, so without explicit wiring those bundled files are
never loaded and pi silently falls back to its stock persona with no error
surfaced.

The reproduction drives the reported journey end to end: register a bundle with
``omnigent server --agent <bundle>``, create a session against it, and send a
message. A marker extension modifies the system prompt via ``before_agent_start``
and the bundle's ``AGENTS.md`` carries a second marker; both markers must appear
in the LLM request the mock server captures. Today neither does.

**Serial execution:** uses the session-scoped mock LLM server like the other
``tests/e2e/omnigent/`` pi rows — do not run under xdist against a shared mock.
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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_EXT_MARKER = "OMNI_PI_BUNDLE_EXT_ACTIVE"
_AGENTSMD_MARKER = "OMNI_PI_BUNDLE_AGENTSMD_MARKER"
_BOOT_TIMEOUT = 60.0
_TURN_TIMEOUT = 180.0

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi bundle-extensions e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_bundle(bundle: Path, model: str) -> None:
    """Write a ``harness: pi`` bundle with a project-local extension + AGENTS.md.

    The extension appends :data:`_EXT_MARKER` to the system prompt from
    ``before_agent_start``; ``AGENTS.md`` carries :data:`_AGENTSMD_MARKER`. Both
    rely on pi's cwd-based project loaders, so both are only visible in the LLM
    request when pi actually runs in the bundle directory.
    """
    ext_dir = bundle / ".pi" / "extensions" / "omni-marker"
    ext_dir.mkdir(parents=True)
    (ext_dir / "index.js").write_text(
        "module.exports = function (pi) {\n"
        '  pi.on("before_agent_start", async (event) => {\n'
        f"    return {{ systemPrompt: `${{event.systemPrompt}}\\n\\n{_EXT_MARKER}` }};\n"
        "  });\n"
        "};\n",
        encoding="utf-8",
    )
    (bundle / "AGENTS.md").write_text(
        f"# Bundle guidance\n\n{_AGENTSMD_MARKER}: always answer like a pirate.\n",
        encoding="utf-8",
    )
    (bundle / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "pi_ext_bundle_agent",
                "prompt": "You are the bundle test agent.",
                "executor": {"model": model, "config": {"harness": "pi"}},
                "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_pi_gateway_config(config_home: Path, *, mock_url: str, model: str) -> None:
    """Point pi at the mock LLM via an OpenAI-key provider (gateway mode)."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auth": {"type": "api_key"},
                "providers": {
                    "mock-oai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "models": {"default": model},
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@contextmanager
def _server_and_runner(
    *,
    python: Path,
    bundle: Path,
    port: int,
    env: dict[str, str],
    cwd: Path,
    db_path: Path,
    runner_id: str,
    binding_token: str,
) -> Iterator[subprocess.Popen[str]]:
    """Spawn ``omnigent server --agent <bundle>`` and a sibling runner."""
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            str(python),
            "-m",
            "omnigent",
            "server",
            "--agent",
            str(bundle),
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
    runner = subprocess.Popen(
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
        yield server
    finally:
        for proc in (runner, server):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _wait_for_online_runner(
    port: int,
    *,
    runner_id: str,
    proc: subprocess.Popen[str],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"server exited with code {proc.returncode}:\n{stdout}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0).status_code == 200:
                status = httpx.get(
                    f"http://127.0.0.1:{port}/v1/runners/{runner_id}/status",
                    timeout=2.0,
                )
                if status.status_code == 200 and status.json().get("online") is True:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    pytest.fail(f"server + runner not ready after {timeout}s")


def _bundle_dir_tarball(bundle: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(str(bundle), arcname=".")
    return buffer.getvalue()


def _create_session(client: httpx.Client, bundle: Path, runner_id: str) -> str:
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _bundle_dir_tarball(bundle), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]
    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    return session_id


def _drive_turn_to_terminal(client: httpx.Client, session_id: str, prompt: str) -> dict[str, Any]:
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }
    client.post(f"/v1/sessions/{session_id}/events", json=body).raise_for_status()
    deadline = time.monotonic() + _TURN_TIMEOUT
    seen_running = False
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"/v1/sessions/{session_id}").json()
        status = last.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        turn_items = [
            item
            for item in last.get("items", [])
            if item.get("type") not in ("resource_event",)
            and not (item.get("type") == "message" and item.get("data", {}).get("role") == "user")
        ]
        if status == "failed" or (status == "idle" and (seen_running or turn_items)):
            return last
        time.sleep(1.0)
    pytest.fail(f"session {session_id} did not become terminal in {_TURN_TIMEOUT}s; last={last}")


def _captured_system_prompts(mock_url: str) -> list[str]:
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=5.0)
    resp.raise_for_status()
    prompts: list[str] = []
    for req in resp.json().get("requests", []):
        if not isinstance(req, dict):
            continue
        for msg in req.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    prompts.append(content)
        instructions = req.get("system") or req.get("instructions")
        if isinstance(instructions, str):
            prompts.append(instructions)
    return prompts


def test_pi_bundle_extensions_reach_session_cwd(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A pi bundle's ``.pi/extensions`` and ``AGENTS.md`` reach the live session.

    Drives the reported journey (register bundle → create session → send a
    message) against a real server + runner + real pi CLI, with the mock LLM
    capturing the outgoing request. The marker extension and ``AGENTS.md`` both
    contribute to the system prompt only when pi loads them from the bundle, so
    their presence in the captured request proves the bundle-root files reached
    pi's actual working directory.
    """
    from omnigent.runner.identity import token_bound_runner_id

    model = f"mock-pi-bundle-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from the mock model."}] * 4,
        key=model,
    )

    bundle = tmp_path / "myagent"
    bundle.mkdir()
    _build_bundle(bundle, model)

    config_home = tmp_path / "omnigent-config"
    _write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    env = dict(mock_credentials_env)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)

    port = _find_free_port()
    db_path = tmp_path / "server.db"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    with _server_and_runner(
        python=omnigent_python,
        bundle=bundle,
        port=port,
        env=env,
        cwd=omnigent_repo_root,
        db_path=db_path,
        runner_id=runner_id,
        binding_token=binding_token,
    ) as server:
        _wait_for_online_runner(port, runner_id=runner_id, proc=server, timeout=_BOOT_TIMEOUT)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            session_id = _create_session(client, bundle, runner_id)
            snapshot = _drive_turn_to_terminal(
                client, session_id, "Introduce yourself in one sentence."
            )

    assert snapshot.get("status") != "failed", (
        f"pi turn failed instead of exercising the extension path: "
        f"{snapshot.get('last_task_error') or snapshot.get('error')}"
    )

    system_prompts = _captured_system_prompts(mock_llm_server_url)
    assert system_prompts, (
        "mock LLM captured no system prompt — the pi turn never reached the "
        "model, so the extension-loading path was not exercised"
    )
    joined = "\n".join(system_prompts)

    assert _EXT_MARKER in joined, (
        "bundle's project-local .pi/extensions extension did not load — its "
        f"before_agent_start marker {_EXT_MARKER!r} is absent from the system "
        "prompt pi sent to the model (pi ran in the session workspace, not the "
        "bundle directory)."
    )
    assert _AGENTSMD_MARKER in joined, (
        "bundle-root AGENTS.md did not reach pi — its marker "
        f"{_AGENTSMD_MARKER!r} is absent from the system prompt (pi's cwd-based "
        "context loader never saw the bundle directory)."
    )
