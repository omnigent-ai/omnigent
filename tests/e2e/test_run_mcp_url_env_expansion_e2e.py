"""``omnigent run ./folder`` must upload MCP ``${VAR}`` references resolved.

Journey: an agent folder declares an MCP server whose ``url`` (sidecar
``tools/mcp/*.yaml`` or inline ``tools.<name>: {type: mcp}``) or stdio ``env``
reads from the user's environment. The user exports the variables and launches
``omnigent run ./company-knowledge/ -p "..."``; the runner must connect to the
resolved server so the model can call its tools.

Usage::

    python -m pytest tests/e2e/test_run_mcp_url_env_expansion_e2e.py -v
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

import httpx
import pytest
import yaml

from tests.e2e.conftest import configure_mock_llm, get_mock_requests, reset_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "tests" / "tools" / "fixtures"
_ECHO_HTTP_MCP_SERVER = _FIXTURES / "echo_http_mcp_server.py"
_ENV_PROBE_STDIO_MCP_SERVER = _FIXTURES / "env_probe_stdio_mcp_server.py"

_AGENT_NAME = "company-knowledge"
_SERVER_NAME = "pipeshub"
_URL_VAR = "PIPESHUB_MCP_URL"
_TOKEN_VAR = "PIPESHUB_MCP_TOKEN"
_REGION_VAR = "PIPESHUB_REGION"
_REGION = "eu-west-1"

# One launch covers daemon spawn, local-server boot, bundle upload, runner
# bring-up and a single mocked tool round-trip (~30s observed).
_RUN_TIMEOUT_S = 240
_STOP_TIMEOUT_S = 120

_SESSION_LINE_RE = re.compile(r"Omnigent session: (?P<base>https?://[^/\s]+)/c/(?P<sid>[0-9a-f]+)")
_PROXY_VARS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})


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
def echo_mcp_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real streamable-HTTP MCP server; ``${PIPESHUB_MCP_URL}`` resolves to its base URL."""
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
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()


def _write_agent_folder(
    root: Path,
    *,
    layout: str,
    mock_llm_server_url: str,
    url: str | None = None,
) -> Path:
    """Write the reporter's ``company-knowledge/`` folder with the MCP server per *layout*."""
    agent_dir = root / _AGENT_NAME
    agent_dir.mkdir()
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": _AGENT_NAME,
        "executor": {
            "type": "omnigent",
            "model": "gpt-4o",
            "config": {"harness": "openai-agents"},
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": f"{mock_llm_server_url}/v1",
            },
        },
        "prompt": "Answer questions from the company knowledge base using the pipeshub tools.",
    }
    http_server = {"url": url, "headers": {"Authorization": f"Bearer ${{{_TOKEN_VAR}}}"}}
    if layout == "sidecar":
        mcp_dir = agent_dir / "tools" / "mcp"
        mcp_dir.mkdir(parents=True)
        (mcp_dir / f"{_SERVER_NAME}.yaml").write_text(
            yaml.safe_dump(
                {"name": _SERVER_NAME, "transport": "http", **http_server}, sort_keys=False
            )
        )
    elif layout == "inline":
        config["tools"] = {_SERVER_NAME: {"type": "mcp", **http_server}}
    elif layout == "inline-stdio":
        config["tools"] = {
            _SERVER_NAME: {
                "type": "mcp",
                "command": sys.executable,
                "args": [str(_ENV_PROBE_STDIO_MCP_SERVER)],
                "env": {_REGION_VAR: f"${{{_REGION_VAR}}}"},
            }
        }
    else:
        raise ValueError(f"unknown layout {layout!r}")
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return agent_dir


def _run_env(home: Path, mock_llm_server_url: str, *, mcp_base_url: str | None) -> dict[str, str]:
    """The user's shell: a fresh HOME, the report's exports, model traffic routed to the mock."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "DATABRICKS_"))
        and key.upper() not in _PROXY_VARS
        and key != "RUNNER_SERVER_URL"
    }
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text("auto_open_conversation: false\n")
    pythonpath = [str(_REPO_ROOT), *[p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]]
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
            "TERM": "dumb",
            _TOKEN_VAR: "pipeshub-token-3f9a",
            _REGION_VAR: _REGION,
        }
    )
    if mcp_base_url is not None:
        env[_URL_VAR] = mcp_base_url
    return env


@contextlib.contextmanager
def _local_omnigent(env: dict[str, str], cwd: Path) -> Iterator[None]:
    """Stop the local server and daemon ``omnigent run`` spawned, even when the test fails."""
    try:
        yield
    finally:
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop"],
            capture_output=True,
            env=env,
            cwd=cwd,
            timeout=_STOP_TIMEOUT_S,
            check=False,
        )


def _omnigent_run(
    agent_dir: Path, env: dict[str, str], prompt: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnigent", "run", f"./{agent_dir.name}/", "-p", prompt],
        capture_output=True,
        text=True,
        env=env,
        cwd=agent_dir.parent,
        timeout=_RUN_TIMEOUT_S,
        check=False,
    )


def _uploaded_mcp_server(stderr: str) -> dict[str, Any]:
    """Read the server-side view of the session's uploaded ``pipeshub`` declaration."""
    match = _SESSION_LINE_RE.search(stderr)
    assert match is not None, f"`omnigent run` never announced a session:\n{stderr[-3000:]}"
    resp = httpx.get(
        f"{match['base']}/v1/sessions/{match['sid']}/agent/mcp-servers",
        timeout=10.0,
        trust_env=False,
    )
    resp.raise_for_status()
    servers = {server["name"]: server for server in resp.json()["data"]}
    assert _SERVER_NAME in servers, f"session has no {_SERVER_NAME!r} MCP server: {servers}"
    return servers[_SERVER_NAME]


def _model_view(mock_llm_server_url: str, token: str) -> tuple[list[str], list[str]]:
    """Return the tools advertised to the model and the tool outputs it received for *token*."""
    tools: list[str] = []
    outputs: list[str] = []
    for request in get_mock_requests(mock_llm_server_url):
        if token not in json.dumps(request.get("input", "")):
            continue
        tools = [str(t.get("name")) for t in request.get("tools") or [] if isinstance(t, dict)]
        items = request.get("input")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                outputs.append(str(item.get("output")))
    return tools, outputs


def _echo_turn(probe: str) -> list[dict[str, Any]]:
    return [
        {
            "tool_calls": [
                {
                    "call_id": "call_pipeshub_1",
                    "name": f"{_SERVER_NAME}__echo",
                    "arguments": json.dumps({"text": probe}),
                }
            ]
        },
        {"text": f"pipeshub says: {probe}"},
    ]


def _drive_echo_journey(
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp_base_url: str,
    *,
    layout: str,
    url: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], list[str], list[str]]:
    """Run the folder once through a pipeshub lookup; return the run, upload and model views."""
    stamp = uuid.uuid4().hex[:8]
    token, probe = f"kb-{layout}-{stamp}", f"probe-{stamp}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, _echo_turn(probe), match=token)
    agent_dir = _write_agent_folder(
        tmp_path, layout=layout, mock_llm_server_url=mock_llm_server_url, url=url
    )
    env = _run_env(tmp_path / "home", mock_llm_server_url, mcp_base_url=echo_mcp_base_url)
    with _local_omnigent(env, tmp_path):
        result = _omnigent_run(agent_dir, env, f"{token} look up {probe} in pipeshub")
        uploaded = _uploaded_mcp_server(result.stderr)
    tools, outputs = _model_view(mock_llm_server_url, token)
    return result, uploaded, tools, outputs


def _assert_pipeshub_echo_worked(
    result: subprocess.CompletedProcess[str],
    uploaded: dict[str, Any],
    tools: list[str],
    outputs: list[str],
    *,
    expected_url: str,
    layout: str,
) -> None:
    combined = result.stdout + result.stderr
    assert uploaded["url"] == expected_url, (
        f"`omnigent run` uploaded the {layout} MCP url unresolved: the session's "
        f"{_SERVER_NAME!r} server declares url {uploaded['url']!r} instead of "
        f"{expected_url!r}.\n{combined[-3000:]}"
    )
    assert f"{_SERVER_NAME}__echo" in tools, (
        f"{_SERVER_NAME}__echo was not advertised to the model, so the runner never "
        f"connected to {expected_url}. tools={tools}\n{combined[-3000:]}"
    )
    assert result.returncode == 0 and "echo: probe-" in "".join(outputs), (
        f"the pipeshub lookup did not round-trip (exit {result.returncode}); "
        f"tool outputs={outputs}\n{combined[-3000:]}"
    )


@pytest.mark.parametrize("layout", ["sidecar", "inline"])
def test_run_uploads_mcp_url_resolved(
    layout: str,
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp_base_url: str,
) -> None:
    """``url: ${PIPESHUB_MCP_URL}/mcp`` must reach the server resolved so the tool works."""
    result, uploaded, tools, outputs = _drive_echo_journey(
        tmp_path,
        mock_llm_server_url,
        echo_mcp_base_url,
        layout=layout,
        url=f"${{{_URL_VAR}}}/mcp",
    )
    _assert_pipeshub_echo_worked(
        result,
        uploaded,
        tools,
        outputs,
        expected_url=f"{echo_mcp_base_url}/mcp",
        layout=layout,
    )


def test_control_literal_mcp_url_reaches_tool(
    tmp_path: Path,
    mock_llm_server_url: str,
    echo_mcp_base_url: str,
) -> None:
    """Same journey with the url spelled literally: the harness can show the passing state."""
    result, uploaded, tools, outputs = _drive_echo_journey(
        tmp_path,
        mock_llm_server_url,
        echo_mcp_base_url,
        layout="sidecar",
        url=f"{echo_mcp_base_url}/mcp",
    )
    _assert_pipeshub_echo_worked(
        result,
        uploaded,
        tools,
        outputs,
        expected_url=f"{echo_mcp_base_url}/mcp",
        layout="sidecar",
    )


def test_run_uploads_inline_stdio_mcp_env_resolved(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Inline stdio ``env: {PIPESHUB_REGION: ${PIPESHUB_REGION}}`` must arrive resolved."""
    token = f"kb-env-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_pipeshub_1",
                        "name": f"{_SERVER_NAME}__read_env",
                        "arguments": json.dumps({"name": _REGION_VAR}),
                    }
                ]
            },
            {"text": "pipeshub region checked"},
        ],
        match=token,
    )
    agent_dir = _write_agent_folder(
        tmp_path, layout="inline-stdio", mock_llm_server_url=mock_llm_server_url
    )
    env = _run_env(tmp_path / "home", mock_llm_server_url, mcp_base_url=None)
    with _local_omnigent(env, tmp_path):
        result = _omnigent_run(agent_dir, env, f"{token} which pipeshub region is configured?")
    combined = result.stdout + result.stderr
    tools, outputs = _model_view(mock_llm_server_url, token)
    assert f"{_SERVER_NAME}__read_env" in tools, (
        f"{_SERVER_NAME}__read_env was not advertised to the model. tools={tools}\n"
        f"{combined[-3000:]}"
    )
    assert f"set:{_REGION}" in "".join(outputs), (
        f"the {_SERVER_NAME} MCP process saw {_REGION_VAR} unresolved: `omnigent run` "
        f"uploaded the inline env literally. tool outputs={outputs}\n{combined[-3000:]}"
    )


def test_run_refuses_launch_when_mcp_url_var_missing(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Without ``PIPESHUB_MCP_URL`` the launch must fail loud instead of uploading the literal."""
    agent_dir = _write_agent_folder(
        tmp_path,
        layout="sidecar",
        mock_llm_server_url=mock_llm_server_url,
        url=f"${{{_URL_VAR}}}/mcp",
    )
    env = _run_env(tmp_path / "home", mock_llm_server_url, mcp_base_url=None)
    with _local_omnigent(env, tmp_path):
        result = _omnigent_run(agent_dir, env, "look up anything in pipeshub")
    combined = result.stdout + result.stderr
    assert result.returncode != 0 and "Unresolved environment variable" in combined, (
        f"`omnigent run` did not report the missing {_URL_VAR} (exit {result.returncode}):\n"
        f"{combined[-3000:]}"
    )
    assert _SESSION_LINE_RE.search(result.stderr) is None, (
        f"a session was created although {_URL_VAR} is unset:\n{combined[-3000:]}"
    )
