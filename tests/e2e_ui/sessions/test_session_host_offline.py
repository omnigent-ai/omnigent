"""Offline native-host refusals leave the real API and browser unconsumed."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.native_session import NativeHarness, create_native_session
from tests._helpers.server_runner import server_runner


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"condition was not met within {timeout}s")


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("runner_bound", [True, False])
@pytest.mark.parametrize(
    ("runner_grace_s", "host_grace_s"),
    [
        pytest.param(1.0, 2.0, id="short-reconnect-grace"),
        pytest.param(None, None, id="production-reconnect-grace", marks=pytest.mark.nightly),
    ],
)
def test_offline_native_host_refuses_input_and_keeps_reconnect_action(
    page: Page,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    harness: NativeHarness,
    runner_bound: bool,
    runner_grace_s: float | None,
    host_grace_s: float | None,
) -> None:
    """A stopped host refuses input after grace and keeps host recovery available.

    PR coverage uses short real waits; nightly retains the production budgets.
    """
    wait_trace = tmp_path / "reconnect-waits.jsonl"
    server_bootstrap = (
        "import json, time\n"
        "from pathlib import Path\n"
        "from omnigent.server.routes import sessions\n"
        "from omnigent.server.routes.sessions import routes_events\n"
        f"host_grace_s = {host_grace_s!r}\n"
        "if host_grace_s is not None:\n"
        "    sessions._HOST_RECONNECT_GRACE_S = host_grace_s\n"
        f"    sessions._HOST_BOUND_RUNNER_CONNECT_GRACE_S = {runner_grace_s!r}\n"
        "def observe_wait(name, wait):\n"
        "    async def observed(*args, **kwargs):\n"
        "        started = time.monotonic()\n"
        "        result = await wait(*args, **kwargs)\n"
        f"        with Path({str(wait_trace)!r}).open('a') as trace:\n"
        "            trace.write(json.dumps({'name': name, 'timeout_s': kwargs['timeout_s'], "
        "'elapsed_s': time.monotonic() - started, 'connected': result is not None}) + '\\n')\n"
        "        return result\n"
        "    return observed\n"
        "sessions._wait_for_runner_client = observe_wait(\n"
        "    'bound_runner', sessions._wait_for_runner_client)\n"
        "routes_events._wait_for_host_reconnect = observe_wait(\n"
        "    'host_reconnect', routes_events._wait_for_host_reconnect)\n"
        "from omnigent.cli import cli\n"
        "cli()\n"
    )
    with (
        server_runner(tmp_path, server_bootstrap=server_bootstrap) as stack,
        httpx.Client(base_url=stack.base_url, timeout=100.0, trust_env=False) as client,
    ):
        stack.start_host(
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "0",
                "OPENAI_API_KEY": "mock-key",
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                "ANTHROPIC_API_KEY": "mock-key",
                "ANTHROPIC_BASE_URL": mock_llm_server_url,
                "CODEX_HOME": str(stack.runner_home / ".codex"),
            }
        )

        def hosts() -> list[dict]:
            response = client.get("/v1/hosts")
            response.raise_for_status()
            return response.json()["hosts"]

        _wait_until(lambda: any(host["status"] == "online" for host in hosts()))
        [host] = [host for host in hosts() if host["status"] == "online"]
        host_id = host["host_id"]
        created = create_native_session(
            client,
            stack.base_url,
            harness=harness,
            metadata={"host_id": host_id, "workspace": str(stack.workspace)},
        )
        session_id = created["session_id"]
        snapshot = client.get(f"/v1/sessions/{session_id}").json()
        runner_id = snapshot["runner_id"]
        assert runner_id is not None
        assert snapshot["host_id"] == host_id

        # Stop the daemon first so no runner-exit report overrides the host cause.
        assert stack.host is not None
        stack.host.kill()
        stack.host.wait(timeout=10.0)
        _, survivors = reap_leaked_omnigent_processes(stack.runner_home / ".omnigent", timeout=2.0)
        assert not survivors
        _wait_until(
            lambda: any(
                host["host_id"] == host_id and host["status"] == "offline" for host in hosts()
            )
        )
        _wait_until(
            lambda: client.get(f"/v1/runners/{runner_id}/status").json()["online"] is False
        )
        if not runner_bound:
            cleared = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": ""})
            cleared.raise_for_status()
            assert client.get(f"/v1/sessions/{session_id}").json()["runner_id"] is None

        marker = f"OFFLINE_HOST_{uuid.uuid4().hex}"
        wait_trace.unlink(missing_ok=True)
        started = time.monotonic()
        response = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": marker}],
                },
            },
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["code"] == "runner_unavailable"
        assert error["message"].startswith("The host for this session is offline.")
        assert " host` (or reconnect it) and send the message again." in error["message"]
        assert "will be sent" not in error["message"]
        expected_host_grace_s = 30.0 if host_grace_s is None else host_grace_s
        expected_waits = [("host_reconnect", expected_host_grace_s)]
        observed = [json.loads(line) for line in wait_trace.read_text().splitlines()]
        assert [(wait["name"], wait["timeout_s"]) for wait in observed] == expected_waits
        # Allow millisecond timer rounding when comparing expiry with monotonic time.
        timer_tolerance_s = 0.01
        assert all(
            wait["elapsed_s"] >= wait["timeout_s"] - timer_tolerance_s for wait in observed
        ), observed
        assert all(not wait["connected"] for wait in observed), observed
        minimum_elapsed = sum(timeout for _, timeout in expected_waits)
        minimum_elapsed -= timer_tolerance_s * len(expected_waits)
        assert minimum_elapsed <= elapsed < 90.0

        items = client.get(f"/v1/sessions/{session_id}/items").json()["data"]
        assert not [item for item in items if item["type"] in {"message", "error"}]
        snapshot = client.get(f"/v1/sessions/{session_id}").json()
        assert snapshot["status"] != "failed"
        assert not snapshot.get("pending_inputs")
        assert f"Host {host_id} is offline; refusing the send for session {session_id}" in (
            stack.log_path("server").read_text()
        )

        page.goto(f"{stack.base_url}/c/{session_id}")
        expect(page.get_by_test_id("composer-host-select")).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-pill")).to_have_count(0)
        expect(page.get_by_role("log").get_by_text(marker, exact=True)).to_have_count(0)
        page.reload()
        expect(page.get_by_test_id("composer-host-select")).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-pill")).to_have_count(0)
        expect(page.get_by_role("log").get_by_text(marker, exact=True)).to_have_count(0)

        held = page.request.post(
            f"{stack.base_url}/v1/sessions/{session_id}/events",
            headers={"x-omnigent-hold-when-host-offline": "on"},
            data={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": marker}]},
            },
            timeout=100_000,
        )
        assert held.status == 503, held.text()
        assert held.json()["error"] == {
            "code": "host_offline",
            "message": (
                "The host for this session is offline. "
                "Your message will be sent when it reconnects."
            ),
        }
        page.get_by_test_id("composer-host-select").click()
        page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
        dialog = page.get_by_test_id("reconnect-session-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_test_id("reconnect-session-command")).to_contain_text(" host")

        output = Path(request.config.getoption("--output"))
        output.mkdir(parents=True, exist_ok=True)
        budget = "production" if host_grace_s is None else "short"
        page.screenshot(
            path=str(output / f"host-offline-{harness}-bound-{runner_bound}-{budget}.png"),
            full_page=True,
        )

        retry = client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "retry_session", "data": {}}
        )
        assert retry.status_code == 503, retry.text
        after_retry = client.get(f"/v1/sessions/{session_id}/items").json()["data"]
        assert [item["id"] for item in after_retry] == [item["id"] for item in items]
        snapshot = client.get(f"/v1/sessions/{session_id}").json()
        assert snapshot["status"] != "failed"
        assert not snapshot.get("pending_inputs")
