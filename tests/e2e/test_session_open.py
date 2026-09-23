"""Exercise the real open CLI, server, runner, and REPL with a mock LLM."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.omnigent._pexpect_harness import submit_prompt

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("restart_runner", [False, True], ids=["connected", "restarted"])
def test_open_preserves_history_and_continues_session(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    restart_live_runner: Callable[[], None],
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    restart_runner: bool,
) -> None:
    """Opening a stored session keeps its identity/history and accepts another turn."""
    model = f"mock-open-{uuid.uuid4().hex}"
    agent_name = register_inline_agent(
        http_client,
        name=model,
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Respond to the user.",
        mock_llm_base_url=f"{isolated_mock_llm_server_url}/v1",
    )
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"text": "OPEN_HISTORY_MARKER"}, {"text": "OPEN_CONTINUED_MARKER"}],
        key=model,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Start this session."
    )
    response = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id
    )
    assert response["status"] == "completed", response.get("error")
    before = http_client.get(f"/v1/sessions/{session_id}")
    before.raise_for_status()
    session_before = before.json()
    history_ids = {item["id"] for item in session_before["items"]}
    catalog_before = http_client.get("/v1/sessions", params={"limit": 1000})
    catalog_before.raise_for_status()
    session_ids_before = {row["id"] for row in catalog_before.json()["data"]}

    if restart_runner:
        restart_live_runner()

    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\nauth:\n  type: api_key\ntui:\n  theme: dark\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OMNIGENT_", "RUNNER_", "DATABRICKS_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_")
        )
        and key not in {"TMUX", "TMUX_PANE"}
    }
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "DATABRICKS_CONFIG_FILE": str(tmp_path / "no-databricks-config"),
            "PYTHONPATH": os.pathsep.join(
                str(path)
                for path in (
                    _REPO_ROOT,
                    _REPO_ROOT / "sdks" / "python-client",
                    _REPO_ROOT / "sdks" / "ui",
                )
            ),
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
        }
    )
    with (tmp_path / "open-terminal.log").open("w") as terminal_log:
        child = pexpect.spawn(
            sys.executable,
            ["-m", "omnigent.cli", "open", session_id, "--server", live_server, "--no-wait"],
            cwd=tmp_path,
            env=env,
            encoding="utf-8",
            timeout=60,
            dimensions=(40, 160),
            logfile=terminal_log,
        )
        try:
            child.expect_exact("OPEN_HISTORY_MARKER")
            child.expect("❯")
            submit_prompt(child, "Continue this session.")
            child.expect_exact("OPEN_CONTINUED_MARKER")
            child.expect("❯")
            child.sendcontrol("d")
            child.expect(pexpect.EOF, timeout=15)
            child.close()
            assert child.exitstatus == 0, (tmp_path / "open-terminal.log").read_text()
        finally:
            if not child.closed:
                child.close(force=True)

    after = http_client.get(f"/v1/sessions/{session_id}")
    after.raise_for_status()
    session_after = after.json()
    assert session_after["id"] == session_id
    assert session_after["runner_id"] == session_before["runner_id"]
    assert session_after.get("host_id") == session_before.get("host_id")
    assert history_ids <= {item["id"] for item in session_after["items"]}
    assert any(
        block.get("text") == "Continue this session."
        for item in session_after["items"]
        if item.get("data", {}).get("role") == "user"
        for block in item["data"].get("content", [])
    ), session_after["items"]
    assert any(
        block.get("text") == "OPEN_CONTINUED_MARKER"
        for item in session_after["items"]
        if item.get("data", {}).get("role") == "assistant"
        for block in item["data"].get("content", [])
    ), session_after["items"]
    catalog_after = http_client.get("/v1/sessions", params={"limit": 1000})
    catalog_after.raise_for_status()
    assert {row["id"] for row in catalog_after.json()["data"]} == session_ids_before
