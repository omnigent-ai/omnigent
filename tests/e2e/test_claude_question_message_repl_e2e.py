"""A real REPL and Claude session preserve questions while web messages wait.

Opt in with OMNIGENT_E2E_CLAUDE_NATIVE=1 and pass --profile plus a real
--llm-api-key. The named profile must provide Claude through Omnigent's
Databricks gateway configuration. No model responses, hooks, transport,
terminal captures, or product functions are replaced.

The first message is typed into ``omnigent attach``. The follow-up uses
the public message API, as the web UI does: typing it into an approval
prompt would submit a verdict and exercise a different path. After verifying
the question survives, the test answers it through the web API and requires
the follow-up to reach Claude's actual transcript.

Run in the background (PROFILE is an explicitly selected Databricks profile)::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 uv run --no-sync pytest \\
        tests/e2e/test_claude_question_message_repl_e2e.py \\
        --profile "$PROFILE" --llm-api-key "$TOKEN" \\
        --basetemp /tmp/question-message-e2e -v -s > /tmp/question-message-e2e.log 2>&1 &

The unfixed checkout fails before the test submits an answer. Evidence, native
Claude transcripts, REPL output, and pane captures remain under --basetemp.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pexpect
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e._native_resume_helpers import (
    inject_user_message,
    wait_for_terminal_ready,
)
from tests.e2e.conftest import build_agent_bundle, find_free_port, wait_for_server
from tests.e2e.test_host_claude_native_e2e import _online_host_id

_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1",
        reason="requires explicit OMNIGENT_E2E_CLAUDE_NATIVE=1 and live Claude credentials",
    ),
    pytest.mark.timeout(480),
]


def _wait_for(check: Callable[[], Any], *, description: str, timeout: float = 120) -> Any:
    """Wait for an observed product outcome; never manufacture a missing event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise pytest.fail.Exception(f"Did not observe {description} within {timeout}s")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop only a process group created by this test."""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def _native_rows(tmp_path: Path) -> list[dict[str, Any]]:
    """Read the real Claude transcripts from this test's isolated config directory."""
    rows = []
    for path in (tmp_path / "claude" / "projects").glob("*/*.jsonl"):
        for line in path.read_text().splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                rows.append(json.loads(line))
    return rows


def _native_tool_results(tmp_path: Path, call_id: str) -> list[dict[str, Any]]:
    """Find native results for the exact model-generated tool call."""
    return [
        row
        for row in _native_rows(tmp_path)
        if any(
            block.get("type") == "tool_result" and block.get("tool_use_id") == call_id
            for block in row.get("message", {}).get("content", [])
            if isinstance(block, dict)
        )
    ]


def _native_user_message(tmp_path: Path, marker: str) -> dict[str, Any] | None:
    """Find a real user message, excluding Claude's user-role tool results."""
    for row in _native_rows(tmp_path):
        if row.get("type") != "user":
            continue
        content = row.get("message", {}).get("content", [])
        if isinstance(content, str) and marker in content:
            return row
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and marker in block.get("text", "")
            for block in content
        ):
            return row
    return None


def _capture_pane(client: httpx.Client, session_id: str) -> str:
    """Observe the runner-owned terminal without sending any input."""
    response = client.get(f"/v1/sessions/{session_id}/resources/terminals/terminal_claude_main")
    response.raise_for_status()
    metadata = response.json()["metadata"]
    return subprocess.check_output(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            "capture-pane",
            "-t",
            metadata["tmux_target"],
            "-p",
            "-S",
            "-100",
        ],
        text=True,
        timeout=10,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _runner_lines(tmp_path: Path) -> list[str]:
    return [
        line
        for log in (tmp_path / "data" / "logs" / "runner").glob("*.log")
        for line in log.read_text().splitlines()
    ]


def _remove_test_history(prompt: str) -> None:
    """Remove only our exact input from the REPL's fixed FileHistory path."""
    path = Path.home() / ".omnigent_history"
    if not path.exists():
        return
    original = path.read_bytes()
    blocks = re.split(rb"(?=\n# )", original)
    kept = [
        block
        for block in blocks
        if b"\n".join(line[1:] for line in block.splitlines() if line.startswith(b"+"))
        != prompt.encode()
    ]
    # Preserve unrelated entries, and leave history alone if a concurrent REPL
    # has appended since our read. Never restore a snapshot of the whole file.
    if len(kept) != len(blocks) and path.read_bytes() == original:
        path.write_bytes(b"".join(kept))


def _environment(tmp_path: Path, *, profile: str, api_key: str) -> dict[str, str]:
    """Isolate product state while retaining access to the selected profile."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "ANTHROPIC_", "CLAUDE_CODE_", "RUNNER_"))
        and not key.startswith("DATABRICKS_")
        and key not in {"CLAUDECODE", "CODEX", "TMUX", "OPENAI_BASE_URL"}
    }
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "tui:\n  theme: dark\n"
        "providers:\n  question-repro:\n    kind: databricks\n    default: true\n"
        f"    profile: {profile}\n"
    )
    claude_config = tmp_path / "claude"
    claude_config.mkdir()
    (claude_config / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(tmp_path / "workspace"): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    env.update(
        OMNIGENT_CONFIG_HOME=str(config),
        OMNIGENT_DATA_DIR=str(tmp_path / "data"),
        OMNIGENT_HOST_ID=uuid.uuid4().hex,
        OMNIGENT_HOST_NAME="question-message-e2e",
        OMNIGENT_RUNNER_ENV_PASSTHROUGH="CLAUDE_CONFIG_DIR",
        OMNIGENT_AUTH_ENABLED="0",
        OMNIGENT_SKIP_ONBOARD="1",
        OMNIGENT_NO_UPDATE_CHECK="1",
        OMNIGENT_SKIP_WEB_UI="1",
        CLAUDE_CONFIG_DIR=str(claude_config),
        DATABRICKS_CONFIG_PROFILE=profile,
        DATABRICKS_CONFIG_FILE=os.environ.get(
            "DATABRICKS_CONFIG_FILE", str(Path.home() / ".databrickscfg")
        ),
        OPENAI_API_KEY=api_key,
        PYTHONPATH=str(_ROOT),
        PATH=f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
        TERM="xterm-256color",
        PROMPT_TOOLKIT_NO_CPR="1",
    )
    return env


def test_web_followup_preserves_real_repl_question(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """A web follow-up must not act as a decline of Claude's pending question."""
    for binary in ("claude", "tmux"):
        assert shutil.which(binary), f"Live reproduction requires {binary} on PATH"
    profile = request.config.getoption("--profile")
    api_key = request.config.getoption("--llm-api-key")
    assert profile, "Pass an explicitly selected --profile for the real Claude gateway"
    assert api_key and api_key != "mock-key", "Pass a real --llm-api-key; no mock fallback"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = _environment(tmp_path, profile=profile, api_key=api_key)
    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"
    question_marker = f"QUESTION_{uuid.uuid4().hex[:12]}"
    followup_marker = f"FOLLOWUP_{uuid.uuid4().hex[:12]}"
    initial_prompt = (
        "Call the built-in AskUserQuestion tool now with exactly one question: "
        f"Which bridge should we use for {question_marker}? "
        "Offer exactly two options: Keep overlay (retain the existing environment) "
        "and Try PYTHONPATH (test a path bridge). Do not use any other tools. "
        "Wait for my selection; do not answer the question yourself."
    )
    evidence: dict[str, Any] = {
        "question_marker": question_marker,
        "followup_marker": followup_marker,
        "claude_version": subprocess.check_output(["claude", "--version"], text=True).strip(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True
        ).strip(),
    }
    processes: list[subprocess.Popen[bytes]] = []
    repl: pexpect.spawn | None = None
    session_id: str | None = None

    def drain_repl() -> None:
        assert repl is not None and repl.isalive(), "The real REPL exited unexpectedly"
        with contextlib.suppress(pexpect.TIMEOUT):
            repl.read_nonblocking(size=65536, timeout=0.05)

    with contextlib.ExitStack() as stack:
        client = stack.enter_context(httpx.Client(base_url=server_url, timeout=30))
        try:
            for name, args in (
                (
                    "server",
                    [
                        "-m",
                        "omnigent.cli",
                        "server",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{tmp_path / 'server.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                ),
                ("host", ["-m", "omnigent.host._daemon_entry", "--server", server_url]),
            ):
                log = stack.enter_context((tmp_path / f"{name}.log").open("w"))
                proc = subprocess.Popen(
                    [sys.executable, *args],
                    env=env,
                    cwd=workspace,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                processes.append(proc)
                if name == "server":
                    wait_for_server(server_url, timeout=45)
            host_id = _online_host_id(client, timeout=45)
            agent_dir = tmp_path / "agent"
            agent_dir.mkdir()
            # A custom native agent is chat-first. The built-in claude-native-ui
            # has a wrapper label that redirects attach into Claude's own TUI.
            (agent_dir / "config.yaml").write_text(
                "spec_version: 1\nname: question-message-repro\n"
                "prompt: Follow the user's instructions.\n"
                "executor:\n  type: omnigent\n  config:\n    harness: claude-native\n"
                "os_env:\n  type: caller_process\n  cwd: .\n  sandbox:\n    type: none\n"
            )
            created = client.post(
                "/v1/sessions",
                data={"metadata": json.dumps({"host_id": host_id, "workspace": str(workspace)})},
                files={
                    "bundle": ("agent.tar.gz", build_agent_bundle(agent_dir), "application/gzip")
                },
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
                timeout=60,
            )
            created.raise_for_status()
            session_id = created.json()["session_id"]
            evidence["session_id"] = session_id
            wait_for_terminal_ready(
                client, conversation_id=session_id, harness="claude", timeout=90
            )
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            assert not snapshot.get("labels", {}).get("omnigent.wrapper"), snapshot

            repl_log = stack.enter_context((tmp_path / "repl.log").open("w"))
            repl = pexpect.spawn(
                sys.executable,
                ["-m", "omnigent.cli", "attach", session_id, "--server", server_url],
                env=env,
                cwd=workspace,
                encoding="utf-8",
                codec_errors="replace",
                dimensions=(50, 180),
                timeout=120,
            )
            repl.logfile_read = repl_log
            repl.expect("❯", timeout=60)
            repl.send(initial_prompt + "\r")

            def pending_question() -> dict[str, Any] | None:
                drain_repl()
                response = client.get(f"/v1/sessions/{session_id}")
                response.raise_for_status()
                return next(
                    (
                        event
                        for event in response.json().get("pending_elicitations", [])
                        if question_marker in json.dumps(event)
                    ),
                    None,
                )

            pending = _wait_for(pending_question, description="a real pending AskUserQuestion")
            assert pending["params"]["tool_name"] == "AskUserQuestion", pending
            evidence["pending_before"] = pending

            # Reading during polling prevents a full PTY from stalling the REPL.
            def approval_displayed() -> bool:
                drain_repl()
                repl_log.flush()
                return "approval required" in (tmp_path / "repl.log").read_text()

            _wait_for(
                approval_displayed,
                description="the generic Omnigent REPL's approval display",
                timeout=10,
            )
            evidence["repl_displayed_approval"] = True
            evidence["pending_observed_at"] = _now()

            def items() -> list[dict[str, Any]]:
                response = client.get(
                    f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
                )
                response.raise_for_status()
                return response.json()["data"]

            call = _wait_for(
                lambda: next(
                    (
                        item
                        for item in items()
                        if item.get("name") == "AskUserQuestion"
                        and question_marker in item.get("arguments", "")
                    ),
                    None,
                ),
                description="the real model's persisted AskUserQuestion call",
            )
            evidence["question_call"] = call
            native_call = _wait_for(
                lambda: next(
                    (
                        row
                        for row in _native_rows(tmp_path)
                        if any(
                            block.get("type") == "tool_use"
                            and block.get("id") == call["call_id"]
                            and block.get("name") == "AskUserQuestion"
                            for block in row.get("message", {}).get("content", [])
                            if isinstance(block, dict)
                        )
                    ),
                    None,
                ),
                description="Claude's original tool_use with the same call ID",
            )
            evidence["native_question_call"] = native_call
            assert native_call["message"].get("model"), native_call
            assert native_call["message"].get("usage", {}).get("output_tokens", 0) > 0, native_call
            (tmp_path / "pane-before.txt").write_text(_capture_pane(client, session_id))
            time.sleep(3)
            before = client.get(f"/v1/sessions/{session_id}").json()
            assert any(
                event["elicitation_id"] == pending["elicitation_id"]
                for event in before.get("pending_elicitations", [])
            ), "Question disappeared before the follow-up; this is not the target reproduction"
            assert not any(
                item.get("type") == "function_call_output"
                and item.get("call_id") == call["call_id"]
                for item in items()
            ), "Question already answered before follow-up"
            assert not _native_tool_results(tmp_path, call["call_id"]), (
                "Claude already resolved the question before the follow-up"
            )

            # Submit no answer until the preservation assertions below have passed.
            evidence["followup_sent_at"] = _now()
            inject_user_message(
                client,
                conversation_id=session_id,
                text=(
                    f"Status check {followup_marker}. "
                    "I have not selected an option yet; this message is not an answer."
                ),
            )
            deadline = time.monotonic() + 45
            # Keep observing through the server's 30s re-park grace. A native
            # cancellation can precede removal of the web pending card.
            while time.monotonic() < deadline:
                current_items = items()
                output = next(
                    (
                        item
                        for item in current_items
                        if item.get("type") == "function_call_output"
                        and item.get("call_id") == call["call_id"]
                    ),
                    None,
                )
                if output is not None and "first_result_observed_at" not in evidence:
                    evidence["first_result_observed_at"] = _now()
                    (tmp_path / "pane-on-cancellation.txt").write_text(
                        _capture_pane(client, session_id)
                    )
                drain_repl()
                time.sleep(0.5)
            evidence["observed_after_followup_at"] = _now()
            # Take fresh snapshots after observation; never assert against a
            # snapshot taken before a delayed native delivery/cancellation.
            evidence["items_after_followup"] = items()
            evidence["session_after_followup"] = client.get(f"/v1/sessions/{session_id}").json()
            (tmp_path / "pane-after.txt").write_text(_capture_pane(client, session_id))
            evidence["native_followup_before_answer"] = _native_user_message(
                tmp_path, followup_marker
            )
            evidence["native_question_results"] = _native_tool_results(tmp_path, call["call_id"])
            output = next(
                (
                    item
                    for item in evidence["items_after_followup"]
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == call["call_id"]
                ),
                None,
            )
            evidence["question_result_after_followup"] = output
            assert output is None and not evidence["native_question_results"], (
                "Ordinary message delivery resolved an unanswered real Claude question. "
                f"The test submitted no answer. Actual tool result: {output!r}. "
                f"Evidence: {tmp_path / 'evidence.json'}"
            )
            assert any(
                event["elicitation_id"] == pending["elicitation_id"]
                for event in evidence["session_after_followup"].get("pending_elicitations", [])
            ), "Follow-up silently removed the pending question"
            assert evidence["session_after_followup"]["runner_online"], "Runner disconnected"
            assert evidence["native_followup_before_answer"] is None, (
                "The follow-up reached Claude before the pending question was answered"
            )

            # Answer the real card only after proving message delivery preserved it.
            questions = pending["params"]["ask_user_question"]["questions"]
            assert len(questions) == 1, questions
            question = questions[0]
            selected_option = question["options"][0]["label"]
            answer = {question["question"]: selected_option}
            evidence["answer_submitted_at"] = _now()
            evidence["submitted_answer"] = answer
            accepted = client.post(
                f"/v1/sessions/{session_id}/elicitations/{pending['elicitation_id']}/resolve",
                json={"action": "accept", "content": answer},
            )
            accepted.raise_for_status()

            def native_answer() -> dict[str, Any] | None:
                drain_repl()
                results = _native_tool_results(tmp_path, call["call_id"])
                return results[0] if results else None

            answered = _wait_for(
                native_answer,
                description="Claude's original tool result containing the selected answer",
                timeout=60,
            )
            evidence["native_result_after_answer"] = answered
            result = next(
                block
                for block in answered["message"]["content"]
                if isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == call["call_id"]
            )
            assert not result.get("is_error"), result
            assert selected_option in json.dumps(result.get("content")), result

            # This liveness check prevents either dropping the web message or
            # leaving it parked forever from making the preservation test pass.
            def delivered_followup() -> dict[str, Any] | None:
                drain_repl()
                return _native_user_message(tmp_path, followup_marker)

            evidence["native_followup_after_answer"] = _wait_for(
                delivered_followup,
                description="the queued web follow-up in Claude's actual user transcript",
                timeout=90,
            )

            def mirrored_followup() -> dict[str, Any] | None:
                drain_repl()
                return next(
                    (
                        item
                        for item in items()
                        if item.get("role") == "user"
                        and followup_marker in json.dumps(item.get("content", []))
                    ),
                    None,
                )

            evidence["mirrored_followup_after_answer"] = _wait_for(
                mirrored_followup,
                description="the delivered follow-up in Omnigent's persisted conversation",
                timeout=30,
            )
            evidence["followup_delivered_at"] = _now()
            final_snapshot = client.get(f"/v1/sessions/{session_id}")
            final_snapshot.raise_for_status()
            evidence["session_after_answer"] = final_snapshot.json()
            assert all(
                event["elicitation_id"] != pending["elicitation_id"]
                for event in final_snapshot.json().get("pending_elicitations", [])
            ), "The answered question is still pending"
            (tmp_path / "pane-after-answer.txt").write_text(_capture_pane(client, session_id))
        finally:
            evidence["captured_before_cleanup_at"] = _now()
            evidence["overlay_dismissals"] = [
                line
                for line in _runner_lines(tmp_path)
                if "dismissing" in line and "covering the input box" in line
            ]
            # The REPL can automatically POST a redundant accept after receiving
            # elicitation_resolved. Preserve its timing; it is not a human answer
            # and must not be confused with the earlier cause of cancellation.
            evidence["verdict_requests"] = [
                line
                for log in (tmp_path / "data" / "logs" / "server").glob("*.log")
                for line in log.read_text().splitlines()
                if "/elicitations/" in line and "/resolve" in line
            ]
            (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
            if repl is not None and repl.isalive():
                repl.terminate(force=True)
            if session_id is not None:
                with contextlib.suppress(httpx.HTTPError):
                    client.delete(
                        f"/v1/sessions/{session_id}/resources/terminals/terminal_claude_main"
                    )
            for proc in reversed(processes):
                _stop(proc)
            _remove_test_history(initial_prompt)
            # The product trust helper still writes this one project entry in the
            # real config despite CLAUDE_CONFIG_DIR. Remove only our temp workspace;
            # preserve the user's other entries and changes made during the test.
            trust_file = Path.home() / ".claude.json"
            if trust_file.exists():
                trust = json.loads(trust_file.read_text())
                if trust.get("projects", {}).pop(str(workspace), None) is not None:
                    trust_file.write_text(json.dumps(trust, indent=2) + "\n")
