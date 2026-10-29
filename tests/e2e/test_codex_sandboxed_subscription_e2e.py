"""A network-denied Codex agent answers through the user's ChatGPT subscription login."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.inner.codex_executor import (
    CODEX_NATIVE_FEATURES_OFF,
    _populate_codex_home_config,
    _strip_host_launched_config,
)
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, get_mock_requests
from tests.e2e.test_polly_codex_subscription_login_e2e import (
    _kill_local_server,
    _polly_env,
    _strip_ansi,
)

pexpect = pytest.importorskip("pexpect")

_PROMPT = "Reply with exactly: OK"
_REPLY_TIMEOUT_S = 240


def _fake_jwt(claims: dict[str, object]) -> str:
    def encode(part: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.c2ln"


def _seed_subscription_home(home: Path, model_base_url: str) -> None:
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "providers:\n"
        "  codex-sub:\n"
        "    kind: subscription\n"
        "    cli: codex\n"
        "    default: true\n",
        encoding="utf-8",
    )
    codex_home = home / ".codex"
    codex_home.mkdir()
    account = {"chatgpt_account_id": "acct", "chatgpt_plan_type": "plus"}
    claims = {"exp": 4102444800, "https://api.openai.com/auth": account}
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _fake_jwt({**claims, "email": "user@example.com"}),
                    "access_token": _fake_jwt(claims),
                    "refresh_token": "fake-refresh-token",
                    "account_id": "acct",
                },
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    # The loopback mock cannot decode Codex's compressed request bodies.
    (codex_home / "config.toml").write_text(
        f'openai_base_url = "{model_base_url}"\n'
        "\n[features]\nenable_request_compression = false\n",
        encoding="utf-8",
    )
    with contextlib.closing(sqlite3.connect(codex_home / "memories_1.sqlite")) as memories:
        memories.execute("VACUUM")


def _write_codex_agent(agent_dir: Path, sandbox_yaml: str) -> None:
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: sandboxed-codex\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: codex\n"
        "prompt: |\n"
        "  Follow the user's instructions exactly.\n"
        "os_env:\n"
        "  type: caller_process\n"
        "  cwd: .\n"
        f"  sandbox:\n{sandbox_yaml}",
        encoding="utf-8",
    )


@pytest.mark.timeout(600)
@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="requires Linux bubblewrap",
)
@pytest.mark.parametrize(
    "sandbox_yaml",
    [
        "    type: none\n",
        "    type: linux_bwrap\n    write_paths: [.]\n    allow_network: false\n",
    ],
    ids=["unsandboxed", "network-denied-bwrap"],
)
def test_codex_agent_answers_with_subscription_login(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    sandbox_yaml: str,
) -> None:
    skip_if_harness_cli_missing("codex")
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": "OK"} for _ in range(20)])
    home = tmp_path / "home"
    _seed_subscription_home(home, f"{isolated_mock_llm_server_url}/v1")
    agent_dir = tmp_path / "agent"
    _write_codex_agent(agent_dir, sandbox_yaml)
    workspace = home / "project"
    workspace.mkdir()

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent.cli", "run", str(agent_dir), "-p", _PROMPT],
        cwd=str(workspace),
        env=_polly_env(home),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(50, 160),
        timeout=_REPLY_TIMEOUT_S,
    )
    try:
        outcome = child.expect(
            [r"(?m)^\s*(?:[●⏺•]\s*)?OK\s*$", r"Error:[^\r\n]*", pexpect.EOF, pexpect.TIMEOUT]
        )
        output = _strip_ansi(child.before + (child.after if isinstance(child.after, str) else ""))
    finally:
        child.close(force=True)
        _kill_local_server(home)

    assert outcome == 0, output[-3000:]


def _offered_tool_names(tools: object) -> set[str]:
    names: set[str] = set()
    for tool in tools if isinstance(tools, list) else []:
        if isinstance(tool, dict):
            names.add(str(tool.get("name", tool.get("type"))))
            names |= _offered_tool_names(tool.get("tools"))
    return names


def _apply_patch_turn(call_id: str, target: Path) -> dict[str, object]:
    patch = f'*** Begin Patch\n*** Add File: {target}\n+{{"replaced": true}}\n*** End Patch\n'
    return {
        "text": "",
        "native_items": [
            {
                "type": "custom_tool_call",
                "id": f"ctc-{call_id}",
                "call_id": call_id,
                "name": "apply_patch",
                "input": patch,
            }
        ],
    }


@pytest.mark.timeout(600)
@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="requires Linux bubblewrap",
)
def test_network_denied_codex_apply_patch_cannot_replace_shared_host_login(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    """Without native tools Codex refuses every apply_patch write and every project MCP server."""
    skip_if_harness_cli_missing("codex")
    home = tmp_path / "home"
    _seed_subscription_home(home, f"{isolated_mock_llm_server_url}/v1")
    host_login = home / ".codex" / "auth.json"
    original_login = host_login.read_text(encoding="utf-8")
    with (home / ".codex" / "config.toml").open("a", encoding="utf-8") as user_config:
        user_config.write(
            f'\n[sandbox_workspace_write]\nwritable_roots = ["{host_login.parent}"]\n'
        )
    workspace = home / "project"
    (workspace / ".codex").mkdir(parents=True)
    mcp_marker = workspace / "mcp-started"
    (workspace / ".codex" / "config.toml").write_text(
        f'[mcp_servers.projprobe]\ncommand = "touch"\nargs = ["{mcp_marker}"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    linked_login = workspace / "linked-login.json"
    linked_login.symlink_to(host_login)
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [
            _apply_patch_turn("patch-host-login", host_login),
            _apply_patch_turn("patch-linked-login", linked_login),
            *({"text": "OK"} for _ in range(20)),
        ],
    )
    agent_dir = tmp_path / "agent"
    _write_codex_agent(
        agent_dir, "    type: linux_bwrap\n    write_paths: [.]\n    allow_network: false\n"
    )

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent.cli", "run", str(agent_dir), "-p", _PROMPT],
        cwd=str(workspace),
        env=_polly_env(home),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(50, 160),
        timeout=_REPLY_TIMEOUT_S,
    )
    try:
        outcome = child.expect(
            [r"(?m)^\s*(?:[●⏺•]\s*)?OK\s*$", r"Error:[^\r\n]*", pexpect.EOF, pexpect.TIMEOUT]
        )
        output = _strip_ansi(child.before + (child.after if isinstance(child.after, str) else ""))
    finally:
        child.close(force=True)
        _kill_local_server(home)

    assert outcome == 0, output[-3000:]
    assert not mcp_marker.exists()
    requests = get_mock_requests(isolated_mock_llm_server_url)
    inputs = [item for request in requests for item in request.get("input", [])]
    offered = _offered_tool_names(
        [tool for request in requests for tool in request.get("tools") or []]
    )
    for item in inputs:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            offered |= _offered_tool_names(item.get("tools"))
    assert "sys_os_shell" in offered, offered
    assert offered.isdisjoint({"exec_command", "shell", "local_shell", "web_search"}), offered
    outputs = {
        item.get("call_id"): str(item.get("output"))
        for item in inputs
        if isinstance(item, dict) and item.get("type") == "custom_tool_call_output"
    }
    assert {"patch-host-login", "patch-linked-login"} <= outputs.keys()
    # Codex's policy, not a failed write, must refuse the user's writable root.
    assert "rejected" in outputs["patch-host-login"], outputs
    assert host_login.read_text(encoding="utf-8") == original_login


@pytest.mark.timeout(180)
def test_stripped_codex_home_ignores_trusted_project_mcp_servers(tmp_path: Path) -> None:
    """A trusted project's ``.codex/config.toml`` cannot add MCP servers back."""
    skip_if_harness_cli_missing("codex")
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text(
        '[mcp_servers.projprobe]\ncommand = "touch"\nargs = ["mcp-started"]\n', encoding="utf-8"
    )
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    (user_home / "config.toml").write_text(
        f'[projects."{project}"]\ntrust_level = "trusted"\n', encoding="utf-8"
    )
    codex_home = tmp_path / "private-codex"
    codex_home.mkdir()
    _populate_codex_home_config(codex_home, user_home)

    def listed_servers() -> str:
        return subprocess.run(
            [shutil.which("codex") or "codex", "mcp", "list"],
            cwd=project,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout

    assert "projprobe" in listed_servers()
    _strip_host_launched_config(codex_home, None, session_id=None)
    assert "projprobe" not in listed_servers()


@pytest.mark.timeout(180)
def test_stripped_codex_home_keeps_apps_off_despite_legacy_alias(tmp_path: Path) -> None:
    """A legacy ``connectors`` feature alias cannot turn Codex apps back on."""
    skip_if_harness_cli_missing("codex")
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    user_config = "[features]\nconnectors = true\n"
    (user_home / "config.toml").write_text(user_config, encoding="utf-8")
    codex_home = tmp_path / "private-codex"
    codex_home.mkdir()
    _populate_codex_home_config(codex_home, user_home)

    def apps_state() -> str:
        argv = [shutil.which("codex") or "codex"]
        for override in CODEX_NATIVE_FEATURES_OFF:
            argv.extend(["-c", override])
        listed = subprocess.run(
            [*argv, "features", "list"],
            cwd=tmp_path,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
        (state,) = [
            line.split()[-1] for line in listed.splitlines() if line.split()[:1] == ["apps"]
        ]
        return state

    assert apps_state() == "true"
    _strip_host_launched_config(codex_home, None, session_id=None)
    assert apps_state() == "false"
    assert (user_home / "config.toml").read_text(encoding="utf-8") == user_config
