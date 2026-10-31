"""Browser e2e: project setup scripts from Project settings to the session notices.

The scripts are typed into Project settings, then two sessions launch on a real
host in the same new worktree. The second launch, arriving while the first is
still installing, must neither run setup again nor start before it ends. The
first run fails, so its notice offers **View setup log** and **Re-run setup**;
after the user fixes the cause, the re-run succeeds on the same runner.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.git_repo import repo_with_worktree
from tests._helpers.host import isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import (
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import get_output_items
from tests.e2e_ui.conftest import configure_mock_llm

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_SHELL_RC = 'uiproj_activate() { export UIPROJ_ENV="$1"; }\n'
_ENV_SCRIPT = "uiproj_activate proj-ui"
_SETUP_SCRIPT = "\n".join(
    [
        "echo installing-ui-deps",
        "echo ran >> setup-runs.txt",
        "sleep 4",
        "touch slept",
        # Fails until the user creates the flag, so the first run shows Re-run setup.
        "test -f allow-setup",
        "echo yes > setup-done",
    ]
)
_PROBE = 'echo "ENV=$UIPROJ_ENV RUNS=$(wc -l < setup-runs.txt | tr -d " ") SLEPT=$(ls slept)"'


def _register_shell_agent(client: httpx.Client, mock_llm_server_url: str) -> tuple[str, str]:
    model = f"mock-ui-setup-{uuid.uuid4().hex[:6]}"
    name = register_inline_agent(
        client,
        name=f"ui-setup-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Call sys_os_shell with exactly the command requested.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            }
        },
    )
    return lookup_agent_id(client, name), model


def _launch(
    client: httpx.Client, base_url: str, host_id: str, session_id: str, workspace: Path
) -> None:
    launch = client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": str(workspace)},
        timeout=90,
    )
    assert launch.status_code == 200, launch.text
    runner_id = launch.json()["runner_id"]
    poll_until(
        lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online") is True,
        timeout_s=60,
    )
    bind_session_runner(client.patch, base_url, session_id, runner_id)


def _probe(client: httpx.Client, mock_url: str, model: str, session_id: str) -> str:
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": uuid.uuid4().hex[:8],
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _PROBE}),
                    }
                ]
            },
            {"text": "done"},
        ],
        key=model,
    )
    response_id = send_user_message_to_session(client, session_id=session_id, content="probe")
    body = poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", body.get("error")
    (output,) = get_output_items(body, "function_call_output")
    return str(json.loads(output["output"])["stdout"])


@pytest.mark.timeout(600)
def test_project_setup_from_settings_to_rerun(
    page: Page, live_server: str, mock_llm_server_url: str, tmp_path: Path
) -> None:
    _, worktree = repo_with_worktree(tmp_path / "src")
    project = f"Setup {uuid.uuid4().hex[:6]}"
    with httpx.Client(base_url=live_server, timeout=20) as client:
        project_id = client.post("/v1/projects", json={"name": project}).json()["id"]
        agent_id, model = _register_shell_agent(client, mock_llm_server_url)
        first = client.post("/v1/sessions", json={"agent_id": agent_id, "project_id": project_id})
        first_id = first.json()["id"]

        # Project settings: type both scripts and save.
        page.goto(f"{live_server}/c/{first_id}")
        page.get_by_role("button", name=f"Project actions for {project}", exact=True).click()
        page.get_by_test_id("project-settings").click()
        expect(page.get_by_test_id("project-settings-save")).to_be_enabled()
        page.get_by_test_id("project-settings-env-commands").fill(_ENV_SCRIPT)
        page.get_by_test_id("project-settings-setup-commands").fill(_SETUP_SCRIPT)
        page.get_by_test_id("project-settings-save").click()
        expect(page.get_by_test_id("project-settings-save")).to_have_count(0)
        config = client.get(f"/v1/projects/{project_id}").json()["config"]
        assert config["env_commands"] == [_ENV_SCRIPT], config
        assert config["setup_commands"] == _SETUP_SCRIPT.split("\n"), config

        home = tmp_path / "host" / "home"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text(_SHELL_RC)
        (home / ".bash_profile").write_text("source ~/.bashrc\n")
        with isolated_host(
            tmp_path / "host",
            live_server,
            env={
                "SHELL": shutil.which("bash"),
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                "OPENAI_API_KEY": "mock-key",
            },
        ) as host:
            host.wait_online(client, timeout=60, poll_interval=0.2)
            # Two sessions start in the new worktree while its setup is running.
            _launch(client, live_server, host.host_id, first_id, worktree)
            second_id = client.post(
                "/v1/sessions", json={"agent_id": agent_id, "project_id": project_id}
            ).json()["id"]
            _launch(client, live_server, host.host_id, second_id, worktree)
            assert not (worktree / "slept").exists(), "both launches land during setup"

            # The second session's first turn waited for the one shared setup run.
            stdout = _probe(client, mock_llm_server_url, model, second_id)
            assert "ENV=proj-ui" in stdout and "RUNS=1" in stdout, stdout
            assert "SLEPT=slept" in stdout, stdout

            # The failed run's notice: read its log, fix the cause, re-run it.
            page.reload()
            headlines = page.get_by_test_id("error-headline")
            expect(headlines.filter(has_text="Project setup running")).to_have_count(1)
            expect(headlines.filter(has_text="Project setup failed")).to_have_count(1)
            log_button = page.get_by_test_id("error-project-setup-log").last
            expect(log_button).to_be_visible(timeout=60_000)
            log_button.click()
            expect(page.get_by_test_id("project-setup-log-content")).to_contain_text(
                "installing-ui-deps", timeout=30_000
            )
            page.keyboard.press("Escape")
            (worktree / "allow-setup").touch()
            rerun = page.get_by_test_id("error-rerun-project-setup").last
            rerun.click()
            expect(rerun).to_have_text("Setup re-running", timeout=30_000)
            poll_until(lambda: (worktree / "setup-done").exists(), timeout_s=60)
            expect(headlines.filter(has_text="Project setup finished")).to_have_count(
                1, timeout=30_000
            )
            assert (worktree / "setup-runs.txt").read_text().split() == ["ran", "ran"]
            stdout = _probe(client, mock_llm_server_url, model, first_id)
            assert "ENV=proj-ui" in stdout and "RUNS=2" in stdout, stdout
