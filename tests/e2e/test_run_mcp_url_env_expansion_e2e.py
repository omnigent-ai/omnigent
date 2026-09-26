"""Real ``omnigent run ./folder`` journey with an MCP server ``url`` taken from ``${VAR}``.
Sidecar ``tools/mcp/*.yaml`` and inline ``type: mcp`` spellings must reach the runner resolved.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import configure_mock_llm, get_mock_requests, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ECHO_HTTP_MCP_SERVER = _REPO_ROOT / "tests" / "tools" / "fixtures" / "echo_http_mcp_server.py"

_AGENT_NAME = "company-knowledge"
_SERVER_NAME = "pipeshub"
_MCP_TOOL = f"{_SERVER_NAME}__echo"
_MCP_URL_VAR = "PIPESHUB_MCP_URL"
_MCP_TOKEN_VAR = "PIPESHUB_MCP_TOKEN"
_MCP_TOKEN = "pipeshub-token-3f9a"

# Launch covers daemon spawn, local-server boot, bundle upload and runner
# bring-up; the turn wait only covers one mocked tool round-trip.
_LAUNCH_TIMEOUT_S = 120
_TURN_TIMEOUT_S = 60

_PROXY_VARS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_listen(port: int, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        time.sleep(0.2)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout_s}s")


@pytest.fixture(scope="module")
def echo_mcp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """A real streamable-HTTP MCP server; yields ``(url, access_log_path)``."""
    port = _free_port()
    log_path = tmp_path_factory.mktemp("echo_mcp") / "echo_mcp.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(_ECHO_HTTP_MCP_SERVER), str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_listen(port)
        yield f"http://127.0.0.1:{port}/mcp", log_path
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()


def _write_agent_folder(root: Path, *, layout: str, url: str, mock_llm_base_url: str) -> Path:
    """Write the reporter's ``company-knowledge/`` folder with the MCP url spelled as *url*."""
    agent_dir = root / _AGENT_NAME
    agent_dir.mkdir()
    headers = {"Authorization": f"Bearer ${{{_MCP_TOKEN_VAR}}}"}
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": _AGENT_NAME,
        "executor": {
            "type": "omnigent",
            "model": "gpt-4o",
            "config": {"harness": "openai-agents"},
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url},
        },
        "prompt": "Answer questions from the company knowledge base using the pipeshub tools.",
    }
    if layout == "inline":
        config["tools"] = {_SERVER_NAME: {"type": "mcp", "url": url, "headers": headers}}
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    if layout == "sidecar":
        mcp_dir = agent_dir / "tools" / "mcp"
        mcp_dir.mkdir(parents=True)
        (mcp_dir / f"{_SERVER_NAME}.yaml").write_text(
            yaml.safe_dump(
                {"name": _SERVER_NAME, "transport": "http", "url": url, "headers": headers},
                sort_keys=False,
            )
        )
    return agent_dir


def _run_env(home: Path, mock_llm_server_url: str, mcp_url: str) -> dict[str, str]:
    """A user's shell with the report's two exports, on a fresh HOME, routed to the mock LLM."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT_") and key.upper() not in _PROXY_VARS
    }
    env.pop("RUNNER_SERVER_URL", None)
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n"
    )
    pythonpath = [str(_REPO_ROOT)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
            _MCP_URL_VAR: mcp_url,
            _MCP_TOKEN_VAR: _MCP_TOKEN,
        }
    )
    return env


def _wait_for_model_request(
    mock_llm_server_url: str, token: str, timeout_s: float = _TURN_TIMEOUT_S
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for request in get_mock_requests(mock_llm_server_url):
            if token in json.dumps(request.get("input", "")):
                return request
        time.sleep(0.5)
    return None


def _tool_names(request: dict[str, Any]) -> list[str]:
    return [
        str(tool.get("name"))
        for tool in request.get("tools") or []
        if isinstance(tool, dict) and tool.get("name")
    ]


def _runner_mcp_log_lines(home: Path) -> str:
    lines: list[str] = []
    for log in sorted((home / ".omnigent" / "logs").rglob("*.log")):
        lines.extend(
            f"{log.name}: {line}"
            for line in log.read_text(errors="replace").splitlines()
            if "mcp" in line.lower()
        )
    return "\n".join(lines[-20:])


def _drive_run(
    agent_dir: Path,
    env: dict[str, str],
    mock_llm_server_url: str,
    *,
    token: str,
    probe: str,
) -> tuple[str, dict[str, Any] | None]:
    """Drive ``omnigent run <folder>`` through one echo-tool turn; returns (REPL text, request)."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_pipeshub_1",
                        "name": _MCP_TOOL,
                        "arguments": json.dumps({"text": probe}),
                    }
                ]
            },
            {"text": f"pipeshub says: {probe}"},
        ],
        match=token,
    )
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "run", f"./{agent_dir.name}/"],
        cwd=str(agent_dir.parent),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=_LAUNCH_TIMEOUT_S,
    )
    transcript: list[str] = []
    request: dict[str, Any] | None = None
    try:
        child.expect(r"company.knowledge", timeout=_LAUNCH_TIMEOUT_S)
        child.expect(r"·\s*ready", timeout=_LAUNCH_TIMEOUT_S)
        transcript.append(child.before or "")
        child.send(f"{token} look up {probe} in pipeshub\r")
        request = _wait_for_model_request(mock_llm_server_url, token)
        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
            child.expect(r"·\s*ready", timeout=_TURN_TIMEOUT_S)
        transcript.append(child.before or "")
        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
            child.expect(pexpect.TIMEOUT, timeout=2)
        transcript.append(child.before or "")
    finally:
        with contextlib.suppress(Exception):
            child.send("/quit\r")
            child.expect(pexpect.EOF, timeout=10)
        child.close(force=True)
    return _strip_ansi("".join(transcript)), request


def _drive_folder(
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp: tuple[str, Path],
    *,
    layout: str,
    url: str,
) -> tuple[str, dict[str, Any] | None, Path]:
    mcp_url, _ = echo_mcp
    agent_dir = _write_agent_folder(
        tmp_path, layout=layout, url=url, mock_llm_base_url=f"{mock_llm_server_url}/v1"
    )
    home = tmp_path / "home"
    env = _run_env(home, mock_llm_server_url, mcp_url)
    stamp = uuid.uuid4().hex[:8]
    output, request = _drive_run(
        agent_dir,
        env,
        mock_llm_server_url,
        token=f"kb-{layout}-{stamp}",
        probe=f"probe-{stamp}",
    )
    return output, request, home


@pytest.mark.parametrize("layout", ["sidecar", "inline"])
def test_run_resolves_mcp_url_from_env(
    layout: str,
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp: tuple[str, Path],
) -> None:
    """``url: ${VAR}`` must reach the runner resolved, so the MCP tools reach the model."""
    mcp_url, mcp_log = echo_mcp
    output, request, home = _drive_folder(
        tmp_path, mock_llm_server_url, echo_mcp, layout=layout, url=f"${{{_MCP_URL_VAR}}}"
    )
    assert request is not None, (
        f"the model never received the turn.\nREPL output:\n{output[-3000:]}"
    )
    tool_names = _tool_names(request)
    assert _MCP_TOOL in tool_names, (
        f"{_MCP_TOOL!r} was not advertised to the model: `omnigent run` uploaded the {layout} MCP "
        f"url as the literal ${{{_MCP_URL_VAR}}} so the runner never connected to {mcp_url}.\n"
        f"tools advertised: {tool_names}\n"
        f"MCP server access log tail:\n{mcp_log.read_text(errors='replace')[-1200:]}\n"
        f"runner log (mcp lines):\n{_runner_mcp_log_lines(home)}\n"
        f"REPL output:\n{output[-3000:]}"
    )


def test_control_literal_mcp_url_reaches_model(
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp: tuple[str, Path],
) -> None:
    """Same journey with the URL spelled literally: the harness can show the passing state."""
    mcp_url, mcp_log = echo_mcp
    output, request, home = _drive_folder(
        tmp_path, mock_llm_server_url, echo_mcp, layout="sidecar", url=mcp_url
    )
    assert request is not None, (
        f"the model never received the turn.\nREPL output:\n{output[-3000:]}"
    )
    tool_names = _tool_names(request)
    assert _MCP_TOOL in tool_names, (
        f"control failed: {_MCP_TOOL!r} missing even with a literal url {mcp_url}.\n"
        f"tools advertised: {tool_names}\n"
        f"MCP server access log tail:\n{mcp_log.read_text(errors='replace')[-1200:]}\n"
        f"runner log (mcp lines):\n{_runner_mcp_log_lines(home)}\n"
        f"REPL output:\n{output[-3000:]}"
    )
