"""Regression test — pi agent dies mid long tool-calling turn.

A pi-harness turn whose tool call runs longer than the executor's
120s stdout idle budget must survive to completion. Today it dies
with ``inner executor error: Pi process ended without response.``:
``omnigent/inner/pi_executor.py`` reads pi's stdout with
``rpc.read_line(timeout=120.0)`` and treats a timeout exactly like
EOF, so a tool that stays silent for >120s (a long build, test
suite, download, ...) kills the turn even though the pi process is
alive and the tool is still running.

Journey (real user path, mock-scripted model):

1. ``omnigent run <agent.yaml> --harness pi -p ...``
2. the model issues one ``long_task`` tool call taking ~150s
3. the tool finishes and the model's final answer (carrying a
   sentinel) must reach stdout — instead the turn errors at ~120s.

Mirrors ``test_yaml_hello_world.py::test_yaml_agent_with_tools[pi]``
(the proven short-tool lane) so the only variable is tool duration.
The long tool lives in a per-test temp module because the shared
``sleep_tool`` example intentionally caps its duration at 10s.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

# The tool sleeps longer than pi_executor's hardcoded 120s
# ``read_line`` idle budget so the turn crosses the failure window.
_TOOL_SLEEP_S = 150

# Sentinel served only in the mock's SECOND response, i.e. only after
# the forced ``long_task`` tool_call round-trips. Its presence in
# stdout proves the long tool-calling turn survived to completion.
_LONG_TOOL_SENTINEL = "LONG_TOOL_TURN_COMPLETED"

_BUG_MESSAGE = "Pi process ended without response"

_PROMPT = "Run the long maintenance task now using the long_task tool."

# Sleep (150s) + harness startup + two mock LLM calls, with headroom.
_RUN_TIMEOUT_SEC = 420


@pytest.mark.timeout(600)
def test_pi_survives_long_tool_calling_turn(
    tmp_path: Path,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    A pi turn with one ~150s tool call completes instead of dying
    with ``Pi process ended without response.`` at the 120s stdout
    idle budget.

    :param tmp_path: Per-test dir hosting the long-tool module and
        agent YAML (the shared example tools cap sleeps at 10s).
    :param omnigent_python: Interpreter with omnigent + pi deps.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Env pointing OPENAI_* at the mock
        LLM server (pi speaks the OpenAI API).
    :param mock_llm_server_url: Base URL of the mock LLM server.
    """
    skip_if_harness_cli_missing("pi")

    mock_model = "mock-pi-long-tool"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_long_task_1",
                        "name": "long_task",
                        "arguments": "{}",
                    }
                ],
            },
            {"text": f"Long task finished. {_LONG_TOOL_SENTINEL}"},
        ],
        key=mock_model,
    )

    # A long tool with no duration cap, plus start/finish markers so a
    # failure distinguishes "tool never ran" from "turn died mid-tool".
    (tmp_path / "long_maintenance_tool.py").write_text(
        textwrap.dedent(
            f"""
            import pathlib
            import time

            _HERE = pathlib.Path(__file__).resolve().parent


            def long_task() -> dict:
                (_HERE / "long_task_started").touch()
                time.sleep({_TOOL_SLEEP_S})
                (_HERE / "long_task_finished").touch()
                return {{"status": "completed"}}
            """
        ),
        encoding="utf-8",
    )
    yaml_path = tmp_path / "long_tool_agent.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            name: long_tool_agent
            prompt: You are an assistant with tools. Use them when asked.

            tools:
              long_task:
                type: function
                description: "Run a long maintenance task (takes a few minutes)."
                callable: long_maintenance_tool.long_task
            """
        ),
        encoding="utf-8",
    )

    env = dict(mock_credentials_env)
    # The YAML's ``callable:`` dotted path lives in tmp_path.
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(tmp_path), env.get("PYTHONPATH", "")) if p)

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            mock_model,
            "--harness",
            "pi",
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    tool_started = (tmp_path / "long_task_started").exists()
    tool_finished = (tmp_path / "long_task_finished").exists()
    combined = result.stdout + result.stderr

    assert tool_started, (
        f"The long_task tool never started — the forced tool_call did not "
        f"reach the tool.\n\nstdout:\n{result.stdout!r}\n\n"
        f"stderr:\n{result.stderr!r}"
    )
    assert _BUG_MESSAGE not in combined, (
        f"Long-tool regression: the pi turn died mid tool call with "
        f"{_BUG_MESSAGE!r} — the 120s stdout idle budget fired while the "
        f"{_TOOL_SLEEP_S}s tool was still running "
        f"(tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode} instead of completing the "
        f"long tool-calling turn (tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # The sentinel lives only in the mock's SECOND response, served only
    # after the ~150s ``long_task`` tool_call executes and its result is
    # sent back — so its presence proves the turn survived end-to-end.
    assert _LONG_TOOL_SENTINEL in result.stdout, (
        f"Final-answer sentinel {_LONG_TOOL_SENTINEL!r} not in stdout; the "
        f"pi harness did not complete the long ({_TOOL_SLEEP_S}s) tool "
        f"round-trip (tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
