"""A real Pi tool call must survive an expired stdout-idle read budget.

PR CI uses a short read budget and tool sleep through the real CLI, harness
subprocess, Pi RPC and mock-provider path. Nightly CI also crosses the production
120-second budget. Both cases observe a real idle read while the tool is running.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

# Sentinel served only in the mock's SECOND response, i.e. only after
# the forced ``long_task`` tool_call round-trips. Its presence in
# stdout proves the long tool-calling turn survived to completion.
_LONG_TOOL_SENTINEL = "LONG_TOOL_TURN_COMPLETED"

_BUG_MESSAGE = "Pi process ended without response"

_PROMPT = "Run the long maintenance task now using the long_task tool."

# Production-duration sleep + harness startup + two mock LLM calls, with headroom.
_RUN_TIMEOUT_SEC = 420


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("idle_timeout_s", "tool_sleep_s"),
    [
        pytest.param(1.0, 4.0, id="short-idle-budget"),
        pytest.param(None, 150.0, id="production-idle-budget", marks=pytest.mark.nightly),
    ],
)
def test_pi_survives_long_tool_calling_turn(
    tmp_path: Path,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    idle_timeout_s: float | None,
    tool_sleep_s: float,
) -> None:
    """An idle read during a real silent tool call must not end the turn."""
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
                time.sleep({tool_sleep_s})
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
    # Patch inside the real harness child, not just its spawning CLI process.
    (tmp_path / "sitecustomize.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            if "omnigent.runtime.harnesses._runner" in sys.orig_argv and "pi" in sys.orig_argv:
                from omnigent.inner import pi_executor

                root = Path(__file__).parent
                idle_timeout_s = {idle_timeout_s!r}
                if idle_timeout_s is not None:
                    pi_executor._TURN_STDOUT_IDLE_TIMEOUT_S = idle_timeout_s
                original_read_line = pi_executor._PiRpcSession.read_line

                async def observe_idle_read(self, timeout=pi_executor._TURN_STDOUT_IDLE_TIMEOUT_S):
                    line = await original_read_line(self, timeout=timeout)
                    if (
                        line is None
                        and self.process is not None
                        and self.process.returncode is None
                        and not self.stdout_at_eof()
                        and (root / "long_task_started").exists()
                        and not (root / "long_task_finished").exists()
                    ):
                        (root / "idle_read_observed").write_text(str(timeout))
                    return line

                pi_executor._PiRpcSession.read_line = observe_idle_read
            """
        ),
        encoding="utf-8",
    )
    # The tool module and harness-only startup shim both live in tmp_path.
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
    idle_read_marker = tmp_path / "idle_read_observed"
    assert idle_read_marker.exists(), (
        f"No stdout-idle timeout was observed while the tool was running.\n{combined}"
    )
    if idle_timeout_s is not None:
        assert float(idle_read_marker.read_text()) == idle_timeout_s
    assert _BUG_MESSAGE not in combined, (
        f"Long-tool regression: the pi turn died mid tool call with "
        f"{_BUG_MESSAGE!r} — the stdout idle budget fired while the "
        f"{tool_sleep_s}s tool was still running "
        f"(tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode} instead of completing the "
        f"long tool-calling turn (tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    # Only the mock's response after the completed tool call carries the sentinel.
    assert _LONG_TOOL_SENTINEL in result.stdout, (
        f"Final-answer sentinel {_LONG_TOOL_SENTINEL!r} not in stdout; the "
        f"pi harness did not complete the long ({tool_sleep_s}s) tool "
        f"round-trip (tool_finished={tool_finished}).\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
