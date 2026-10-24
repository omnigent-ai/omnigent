"""PTY e2e: ``omnigent run --resume`` must replay a tool call whose arguments look
like Rich markup (``echo '[/dim]'``) literally and reach the prompt instead of crashing.
Runs the real CLI in local-daemon mode against the mock LLM."""

from __future__ import annotations

import contextlib
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests.e2e._native_resume_helpers import cli_env, omnigent_console_script
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

from tests.e2e.omnigent._pexpect_harness import (  # noqa: E402
    STATE_SLEEPING,
    await_turn_complete,
    clean_exit,
    strip_ansi,
    submit_prompt,
    wait_for_ready,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_YAML = (
    _REPO_ROOT / "tests" / "_fixtures" / "agents" / "markup-args-shell" / "markup-args-shell.yaml"
)
_MODEL_KEY = "mock-markup-args-shell"
_SHELL_COMMAND = "echo '[/dim]'"
# The replayed history echoes the user's "❯ …" line before the tool call renders,
# so wait on the status bar rather than the harness's PROMPT_READY marker.
_READY_PATTERNS = [STATE_SLEEPING, r"·\s*ready"]
_BOOT_TIMEOUT_S = 180.0
_RESUME_TIMEOUT_S = 120.0
_PTY_ROWS, _PTY_COLS = 40, 160


@pytest.fixture
def repl_env(mock_llm_server_url: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    """Isolated HOME/config/data for a local-daemon ``omnigent run`` pointed at the mock LLM."""
    home = tmp_path / "home"
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        f"auto_open_conversation: false\ntui:\n  theme: light\ndefault_agent: {_AGENT_YAML}\n",
        encoding="utf-8",
    )
    env = cli_env()
    # This process may itself run inside an Omnigent session; keep its database
    # and session ids away from the isolated local daemon.
    for stale in ("OMNIGENT_DATABASE_URI", "OMNIGENT_SESSION_ID"):
        env.pop(stale, None)
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(config_home),
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "PROMPT_TOOLKIT_NO_CPR": "1",
            "LINES": str(_PTY_ROWS),
            "COLUMNS": str(_PTY_COLS),
        }
    )
    try:
        yield env
    finally:
        _, survivors = reap_leaked_omnigent_processes(config_home)
        assert not survivors, f"REPL test processes survived cleanup: {survivors}"


def _spawn(args: list[str], env: dict[str, str], transcript: io.StringIO) -> Any:
    child = pexpect.spawn(
        str(omnigent_console_script()),
        args,
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(_PTY_ROWS, _PTY_COLS),
        timeout=_BOOT_TIMEOUT_S,
    )
    child.logfile_read = transcript
    return child


def _create_conversation_with_markup_tool_call(env: dict[str, str]) -> str:
    """Drive one turn whose tool call contains ``[/dim]``; return the conversation id."""
    transcript = io.StringIO()
    child = _spawn(["run", str(_AGENT_YAML)], env, transcript)
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT_S)
        submit_prompt(child, f"run {_SHELL_COMMAND}")
        await_turn_complete(child, running_timeout=60, completion_timeout=120)
        clean_exit(child, timeout=60)
    finally:
        if child.isalive():
            child.terminate(force=True)
    plain = re.sub(r"\\\s*\n\s*", " ", strip_ansi(transcript.getvalue()))
    match = re.search(r"--resume\s+([0-9a-f]{32}|conv_[0-9a-f]+)", plain)
    assert match, f"no resume hint with a conversation id in the exit output:\n{plain[-3000:]}"
    return match.group(1)


def test_resume_replays_tool_call_with_markup_like_args(
    repl_env: dict[str, str], mock_llm_server_url: str
) -> None:
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call-dim-1",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _SHELL_COMMAND}),
                    }
                ]
            },
            {"text": "Done."},
        ],
        key=_MODEL_KEY,
    )
    conversation_id = _create_conversation_with_markup_tool_call(repl_env)

    transcript = io.StringIO()
    child = _spawn(["run", "--resume", conversation_id], repl_env, transcript)
    try:
        idx = child.expect([*_READY_PATTERNS, r"\[Y/n\]", pexpect.EOF], timeout=_RESUME_TIMEOUT_S)
        if idx == 2:
            # Decline the crash handler's bug-filing prompt; a bare Enter would accept it.
            child.send("n\r")
            with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
                child.expect(pexpect.EOF, timeout=30)
        plain = strip_ansi(transcript.getvalue())
        assert idx in (0, 1), (
            f"`omnigent run --resume {conversation_id}` never reached the prompt. "
            f"Output tail:\n{plain[-3000:]}"
        )
        assert "MarkupError" not in plain, f"resume raised MarkupError:\n{plain[-3000:]}"
        assert re.search(r"⏵ sys_os_shell\([^\n]*echo '\[/dim\]'", plain), (
            f"replayed history lacks the tool-call line with literal arguments:\n{plain[-3000:]}"
        )
        clean_exit(child, timeout=60)
    finally:
        if child.isalive():
            child.terminate(force=True)
