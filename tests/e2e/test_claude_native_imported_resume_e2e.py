"""Import and resume a held transcript through real Claude, CLI and runner processes.

Only the model and the background holder's registry entry are scripted. Claude's
own refusal, transcript loading, hooks and Omnigent delivery run normally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests._helpers.live_server import isolated_process_env, terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import send_user_text
from tests.e2e._native_resume_helpers import (
    poll_for_assistant_marker,
    spawn_cli_background,
    wait_for_terminal_ready,
)
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5"


@pytest.mark.posix_only
@pytest.mark.timeout(240)
@pytest.mark.parametrize("launch_path", ["cli", "runner"])
def test_imported_background_session_resumes_without_reimporting_its_copy(
    tmp_path: Path, isolated_mock_llm_server_url: str, launch_path: str
) -> None:
    """Both resume owners preserve history and the live source without a duplicate import."""
    claude = shutil.which("claude")
    if claude is None or shutil.which("tmux") is None:
        pytest.skip("requires real Claude Code and tmux")
    if any(
        path.exists()
        for path in (
            Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        )
    ):
        pytest.skip("requires a clean environment without machine-wide Claude routing")

    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "mock": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": isolated_mock_llm_server_url,
                            "api_key": "local-test-key",
                            "models": {"default": _MODEL},
                        },
                    }
                },
            }
        )
    )
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    runtime_env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(claude_config),
        "ANTHROPIC_BASE_URL": isolated_mock_llm_server_url,
        "ANTHROPIC_API_KEY": "local-test-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "TERM": "xterm-256color",
        "PYTHONPATH": str(_REPO),
    }
    base_env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG"}}
    with (
        ExitStack() as cleanup,
        server_runner(
            tmp_path / "stack",
            base_env=base_env,
            server_env=runtime_env,
            server_cwd=_REPO,
            poll_interval=0.1,
        ) as stack,
    ):
        claude_config.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "customApiKeyResponses": {"approved": ["local-test-key"]},
                    "projects": {str(stack.workspace): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        source_id = str(uuid.uuid4())
        source = (
            claude_config
            / "projects"
            / re.sub(r"[^a-zA-Z0-9]", "-", str(stack.workspace))
            / f"{source_id}.jsonl"
        )
        source.parent.mkdir(parents=True)
        history = f"held-history-{uuid.uuid4().hex}"
        source.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": str(uuid.uuid4()),
                    "parentUuid": None,
                    "sessionId": source_id,
                    "cwd": str(stack.workspace),
                    "message": {"role": "user", "content": history},
                }
            )
            + "\n"
        )
        source_bytes = source.read_bytes()
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(240)"])
        cleanup.callback(terminate_process, holder)
        registry = claude_config / "sessions" / f"{holder.pid}.json"
        registry.parent.mkdir()
        registry.write_text(json.dumps({"sessionId": source_id, "kind": "bg", "jobId": "held"}))
        registry_bytes = registry.read_bytes()
        cli_env = isolated_process_env(stack.runner_home, runtime_env, base_env)
        cli_env["OMNIGENT_DATA_DIR"] = str(stack.root)

        refused = spawn_cli_background(
            [claude, "--resume", source_id], env=cli_env, cwd=str(stack.workspace)
        )
        cleanup.callback(refused.terminate)
        try:
            poll_until(
                lambda: "background" in refused.output() and "--fork-session" in refused.output(),
                timeout_s=30,
            )
        finally:
            (tmp_path / "refused-terminal.txt").write_text(refused.output())
        refused.terminate()

        def import_cli(*selection: str, server: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "import",
                    "--harness",
                    "claude",
                    *selection,
                    "--server",
                    server,
                ],
                env=cli_env,
                cwd=stack.workspace,
                capture_output=True,
                text=True,
                timeout=45,
            )

        imported = import_cli("--session", source_id, server=stack.base_url)
        assert imported.returncode == 0, imported.stdout + imported.stderr
        match = re.search(r"/c/(\S+)", imported.stdout)
        assert match is not None, imported.stdout
        session_id = match.group(1)
        base_url = stack.base_url
        if launch_path == "cli":
            terminate_process(stack.server)
            running = spawn_cli_background(
                [sys.executable, "-m", "omnigent", "claude", "--resume", session_id],
                env=cli_env,
                cwd=str(stack.workspace),
            )
            cleanup.callback(running.terminate)
            try:
                poll_until(lambda: "Web UI: http://127.0.0.1:" in running.output(), timeout_s=90)
            finally:
                (tmp_path / "omnigent-cli.txt").write_text(running.output())
            launched = re.search(r"Web UI: (http://127\.0\.0\.1:\d+)", running.output())
            assert launched is not None, running.output()
            base_url = launched.group(1)
        else:
            stack.start_runner(env=runtime_env, cwd=_REPO)

        client = cleanup.enter_context(
            httpx.Client(base_url=base_url, trust_env=False, timeout=60)
        )
        if launch_path == "runner":
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
            ).raise_for_status()
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            ).raise_for_status()
        wait_for_terminal_ready(client, conversation_id=session_id, harness="claude", timeout=60)
        prompt, reply = f"continue-{uuid.uuid4().hex}", f"resumed-{uuid.uuid4().hex}"
        configure_mock_llm(isolated_mock_llm_server_url, [], match=prompt)
        set_fallback_mock_llm(isolated_mock_llm_server_url, prompt, reply)
        send_user_text(client, session_id=session_id, text=prompt)
        poll_for_assistant_marker(client, conversation_id=session_id, marker=reply, timeout=60)
        requests = [
            request
            for request in get_mock_requests(isolated_mock_llm_server_url)
            if request.get("stream") and prompt in json.dumps(request.get("messages"))
        ]
        assert requests and history in json.dumps(requests[-1]["messages"])
        copies = [path for path in source.parent.glob("*.jsonl") if path != source]
        assert len(copies) == 1, copies
        assert source.read_bytes() == source_bytes
        assert registry.read_bytes() == registry_bytes
        assert holder.poll() is None
        assert not (stack.runner_home / ".claude" / "projects").exists()
        repeated = import_cli("--last", "10", server=base_url)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert "Imported: 0" in repeated.stdout and "Already imported: 1" in repeated.stdout
        explicit_copy = import_cli("--session", copies[0].stem, server=base_url)
        assert explicit_copy.returncode != 0, explicit_copy.stdout
        resources = client.get(f"/v1/sessions/{session_id}/resources").json()["data"]
        terminal = next(resource for resource in resources if resource["type"] == "terminal")
        metadata = terminal["metadata"]
        pane = subprocess.check_output(
            [
                "tmux",
                "-S",
                metadata["tmux_socket"],
                "capture-pane",
                "-p",
                "-S",
                "-200",
                "-t",
                metadata["tmux_target"],
            ],
            text=True,
        )
        (tmp_path / "resumed-terminal.txt").write_text(pane)
        (tmp_path / "verification.txt").write_text(
            f"entry={launch_path}\nsource={source_id}\ncopy={copies[0].stem}\n"
            "history reached mock; reply persisted; source and holder unchanged\n"
            f"{repeated.stdout}"
        )
