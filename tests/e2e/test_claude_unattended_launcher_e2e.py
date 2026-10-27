"""Real server/runner/tmux boundary with a synthetic gateway launcher."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires real tmux")
@pytest.mark.timeout(180)
def test_unattended_native_launch_reaches_gateway_without_prompt(tmp_path: Path) -> None:
    """A web terminal ensure passes the opt-in through to the actual child."""
    receipt = tmp_path / "launcher.json"
    launcher = tmp_path / "claude"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('2.1.309 (synthetic launcher)'); sys.exit(0)\n"
        f"Path({str(receipt)!r}).write_text(json.dumps({{\n"
        "    'flag': os.environ.get('UG_NONINTERACTIVE'),\n"
        "    'home': os.environ['HOME'], 'args': sys.argv[1:],\n"
        "    'pid': os.getpid(), 'tty': sys.stdin.isatty(),\n"
        "}))\n"
        "if os.environ.get('UG_NONINTERACTIVE') != '1':\n"
        "    print('Synthetic gateway would wait for interactive sign-in', flush=True)\n"
        "else:\n"
        "    print('Synthetic gateway skipped interactive prompts', flush=True)\n"
        "    print('─' * 40 + '\\n❯ \\n' + '─' * 40, flush=True)\n"
        "time.sleep(120)\n"
    )
    launcher.chmod(0o755)
    base_env = {name: os.environ[name] for name in ("PATH", "LANG") if name in os.environ}
    with server_runner(tmp_path / "stack", base_env=base_env) as stack:
        stack.start_runner(
            env={
                "OMNIGENT_CLAUDE_PATH": str(launcher),
                "CLAUDE_CONFIG_DIR": str(stack.runner_home / ".claude"),
                "ANTHROPIC_API_KEY": "synthetic-key",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            }
        )
        with httpx.Client(
            base_url=stack.base_url,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=60,
            trust_env=False,
        ) as client:
            session = create_native_session(
                client,
                stack.base_url,
                harness="claude",
                metadata={"workspace": str(stack.workspace)},
            )
            session_id = session["session_id"]
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
            ).raise_for_status()
            poll_until(receipt.exists, timeout_s=60)
            observed = json.loads(receipt.read_text())
            assert observed["flag"] == "1", observed
            assert observed["home"] == str(stack.runner_home), observed
            assert observed["tty"] is True, observed
            assert "--settings" in observed["args"], observed
            ensure = client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            )
            assert ensure.status_code == 200, ensure.text + stack.log_tail()
            resources = client.get(f"/v1/sessions/{session_id}/resources")
            resources.raise_for_status()
            assert any(row["type"] == "terminal" for row in resources.json()["data"])
