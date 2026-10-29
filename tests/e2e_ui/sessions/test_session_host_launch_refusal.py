"""An online host that refuses a runner launch keeps its startup reason.

Unlike an offline host (``test_session_host_offline.py``), the daemon stays
online but every runner spawn fails for an uncoded reason (a missing
interpreter), so the native send and Resume/retry must both name it.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.compat import COMPAT_RUNNER_PYTHON_ENV
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests.e2e_ui.sessions._host_stack import list_hosts, wait_for_online_host


def _build_removable_interpreter(dst: Path) -> Path:
    """A venv whose ``bin/python`` symlink can be removed to break spawns.

    The daemon captures this interpreter as ``sys.executable`` and spawns each
    runner with it, so deleting the symlink once the daemon is online leaves the
    host online while every subsequent launch fails at spawn.
    """
    real_venv = Path(sys.prefix)
    if not (real_venv / "pyvenv.cfg").is_file():
        pytest.skip("requires running inside a venv to mirror its interpreter")
    (dst / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real_venv / "pyvenv.cfg", dst / "pyvenv.cfg")
    (dst / "lib").symlink_to(real_venv / "lib")
    if (real_venv / "lib64").exists():
        (dst / "lib64").symlink_to(real_venv / "lib64")
    interpreter = dst / "bin" / "python"
    interpreter.symlink_to(os.path.realpath(real_venv / "bin" / "python"))
    return interpreter


def test_online_host_launch_refusal_surfaces_startup_reason(
    request: pytest.FixtureRequest,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = _build_removable_interpreter(tmp_path / "mirror")
    monkeypatch.setenv(COMPAT_RUNNER_PYTHON_ENV, str(interpreter))

    with (
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(base_url=stack.base_url, timeout=100.0, trust_env=False) as client,
    ):
        stack.start_host(
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "0",
                "ANTHROPIC_API_KEY": "mock-key",
                "ANTHROPIC_BASE_URL": mock_llm_server_url,
            }
        )

        host_id = wait_for_online_host(client)

        # Break every runner spawn while the daemon keeps running.
        interpreter.unlink()
        assert any(
            host["host_id"] == host_id and host["status"] == "online"
            for host in list_hosts(client)
        ), "host must stay online after its interpreter is removed"

        created = create_native_session(
            client,
            stack.base_url,
            harness="claude",
            metadata={"host_id": host_id, "workspace": str(stack.workspace)},
        )
        session_id = created["session_id"]
        assert client.get(f"/v1/sessions/{session_id}").json()["host_id"] == host_id

        # Open the session page before sending so the first message is the
        # real composer send.
        page = request.getfixturevalue("page")
        page.goto(f"{stack.base_url}/c/{session_id}")
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-pill")).to_have_count(0)

        marker = f"SPAWN_REFUSAL_{uuid.uuid4().hex}"
        composer.click()
        composer.fill(marker)
        composer.press("Enter")

        pill = page.get_by_test_id("error-pill")
        expect(pill).to_be_visible(timeout=90_000)
        expect(page.get_by_role("log").get_by_text(marker, exact=True)).to_be_visible()
        expect(pill.get_by_role("button", name="Resume session")).to_be_visible()
        pill.locator("button[aria-expanded]").click()

        items = client.get(f"/v1/sessions/{session_id}/items").json()["data"]
        [send_error] = [item for item in items if item["type"] == "error"]
        assert send_error["code"] == "runner_failed_to_start"
        send_message = send_error["message"]
        expect(page.get_by_test_id("error-message-content")).to_have_text(send_message)

        # The failed turn names the host's own spawn failure, not the fallback.
        assert "failed to spawn runner" in stack.log_path("host").read_text()
        assert send_message.startswith("The host could not start a runner for this session")
        assert "failed to spawn runner" in send_message
        assert "may have failed to start" not in send_message

        output = Path(request.config.getoption("--output"))
        output.mkdir(parents=True, exist_ok=True)
        if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            page.wait_for_timeout(2500)  # hold the failed-turn copy readable in the clip
        page.screenshot(path=str(output / "facet1-native-send.png"), full_page=True)

        with page.expect_response(
            lambda result: (
                result.request.method == "POST"
                and result.url.endswith(f"/v1/sessions/{session_id}/events")
                and result.request.post_data_json.get("type") == "retry_session"
            ),
            timeout=100_000,
        ) as retry:
            pill.get_by_role("button", name="Resume session").click()
        assert retry.value.status == 503
        retry_message = retry.value.json()["error"]["message"]
        expect(page.get_by_text(f"Resume session failed: {retry_message}")).to_be_visible()
        # Retry explains the same refusal; the host's reason is identical.
        assert retry_message == send_message
        if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            page.wait_for_timeout(2500)  # hold the retry failure readable in the clip
        page.screenshot(path=str(output / "facet2-retry.png"), full_page=True)
