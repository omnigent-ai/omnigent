"""Actual guided PTY regression with a local dummy executable and strict guard."""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from tests._helpers.provider_setup_terminal import ProviderSetupTerminalRuntime


@pytest.fixture
def guided_runtime(tmp_path: Path, built_spa: None) -> Iterator[ProviderSetupTerminalRuntime]:
    tmux = os.environ.get("PROVIDER_GUIDED_FIXTURE_TMUX")
    if not tmux:
        pytest.skip("Set PROVIDER_GUIDED_FIXTURE_TMUX to an explicitly reviewed tmux executable")
    runtime = ProviderSetupTerminalRuntime(
        tmp_path / "guided", Path(__file__).resolve().parents[3], tmux=Path(tmux)
    )
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()


@pytest.fixture
def guided_recordings(tmp_path: Path) -> Path:
    path = tmp_path / "guided-recordings"
    path.mkdir()
    return path


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Fixture condition timed out")


def assert_terminal_text_painted(terminal):
    for attempt in range(20):
        pixels = Image.open(io.BytesIO(terminal.locator(".xterm-screen").screenshot())).convert(
            "RGB"
        )
        background = sum(pixels.getpixel((0, 0))) / 3
        contrast = sum(
            max(pixel) - min(pixel) < 25 and abs(sum(pixel) / 3 - background) > 60
            for pixel in pixels.get_flattened_data()
        )
        if contrast > 300:
            return contrast
        terminal.page.wait_for_timeout(250)
    raise AssertionError("Terminal prompt bytes arrived but visible text was not painted")


def exercise_guided_timeout(
    page: Page, runtime: ProviderSetupTerminalRuntime, recordings: Path
) -> None:
    """A real idle fixture child times out and releases the selected host's write guard."""
    page.goto(runtime.url + "/settings/providers")
    page.get_by_test_id("settings-providers-host").click()
    page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
    page.get_by_test_id("setup-agent-codex").click()
    page.get_by_role("button", name="ChatGPT subscription", exact=True).click()
    terminal = page.get_by_role("region", name="Provider setup terminal")
    expect(terminal).to_have_attribute("data-operation-state", "failed", timeout=15000)
    expect(terminal).to_contain_text("Setup operation timed out.")
    operation = terminal.get_attribute("data-operation-id")
    sockets = [
        json.loads(line)
        for line in (runtime.root / "terminal-fixture-sockets.jsonl").read_text().splitlines()
    ]
    owned = next(row for row in sockets if row["operation_id"] == operation)
    wait_for(lambda: not Path(owned["socket"]).exists())
    rows = [
        json.loads(line)
        for line in (runtime.root / "terminal-fixture-inputs.jsonl").read_text().splitlines()
    ]
    pid = rows[0]["pid"]

    def child_gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(child_gone)
    api = runtime.url + "/v1/hosts/11111111111141118111111111111111"
    inventory = httpx.get(api + "/setup", timeout=10).json()
    assert not any(row["kind"] == "subscription" for row in inventory["providers"])
    response = httpx.post(
        api + "/setup/actions",
        json={"action": "set_default", "name": "fixture-secondary", "surface": "openai"},
        timeout=10,
    )
    response.raise_for_status()
    assert response.json()["inventory"]["effective_defaults"]["openai"] == "fixture-secondary"
    page.screenshot(path=str(recordings / "timed-out.png"), full_page=True)
    (recordings / "timeout-proof.json").write_text(
        json.dumps(
            {
                "fixture_timeout_seconds": 3,
                "operation_id": operation,
                "state": "failed",
                "error": "Setup operation timed out.",
                "child_and_socket_removed": True,
                "subscription_not_persisted": True,
                "subsequent_save_status": response.status_code,
                "new_default": "fixture-secondary",
            },
            indent=2,
        )
    )


def test_guided_prompt_reload_reconnect_and_scoped_cleanup(
    page: Page,
    guided_runtime: ProviderSetupTerminalRuntime,
    guided_recordings: Path,
) -> None:
    STATE = guided_runtime.root
    RECORD = guided_recordings
    URL = guided_runtime.url
    LOG = STATE / "terminal-fixture-inputs.jsonl"
    RECORD.mkdir(parents=True, exist_ok=True)

    def rows():
        return [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []

    page.add_init_script(
        """
        window.fixtureSockets = [];
        window.fixtureOutput = '';
        const Original = window.WebSocket;
        window.WebSocket = class extends Original {
            constructor(...args) {
                super(...args);
                if (String(args[0]).includes('setup-operations')) {
                    window.fixtureSockets.push(this);
                    this.addEventListener('message', event => {
                        if (event.data instanceof ArrayBuffer) {
                            window.fixtureOutput += new TextDecoder().decode(event.data);
                        }
                    });
                }
            }
        };
        """
    )
    before = len(rows())
    delayed_starts = []
    other_operation = None

    def delay_start(route):
        if route.request.method != "POST":
            route.continue_()
            return
        response = route.fetch()
        if response.ok:
            delayed_starts.append(response.json())
            if response.json()["state"] == "running":
                wait_for(lambda: any(row["event"] == "prompt_written" for row in rows()[before:]))
        route.fulfill(response=response)

    page.route("**/setup-operations", delay_start)
    try:
        page.goto(URL + "/settings/providers")
        page.get_by_test_id("settings-providers-host").click()
        page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
        page.get_by_test_id("setup-agent-codex").click()
        page.get_by_role("button", name="ChatGPT subscription", exact=True).click()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-state", "running", timeout=20000)
        terminal.scroll_into_view_if_needed()
        remote_help = terminal.get_by_text("a localhost redirect opens", exact=False)
        expect(remote_help).to_be_hidden()
        terminal.get_by_text("Sign-in help", exact=True).click()
        expect(remote_help).to_be_visible()
        expect(remote_help).to_contain_text("Use device-code sign-in when available")
        page.screenshot(path=str(RECORD / "remote-sign-in-help.png"))
        terminal.get_by_text("Sign-in help", exact=True).click()
        page.wait_for_function(
            "window.fixtureOutput.includes('Device code: TEST-1234')", timeout=20000
        )
        terminal.scroll_into_view_if_needed()
        assert_terminal_text_painted(terminal)
        page.screenshot(path=str(RECORD / "pre-attach-prompt.png"))
        operation_id = terminal.get_attribute("data-operation-id")
        api = URL + "/v1/hosts/11111111111141118111111111111111"
        rejected_requests = []
        for payload in (
            {"action": "codex-login", "command": "fixture-unapproved-command"},
            {"action": "codex-login", "parameters": {"cwd": str(STATE)}},
            {"action": "codex-login", "parameters": {"env": {"FIXTURE": "value"}}},
        ):
            response = httpx.post(api + "/setup-operations", json=payload, timeout=10)
            rejected_requests.append(
                {"payload": payload, "status": response.status_code, "response": response.json()}
            )
            assert response.status_code in (400, 422), rejected_requests[-1]
        conflict = httpx.post(
            api + "/setup-operations", json={"action": "codex-login"}, timeout=10
        )
        assert conflict.status_code == 409
        blocked_save = httpx.post(
            api + "/setup/actions",
            json={"action": "set_default", "name": "fixture-secondary", "surface": "openai"},
            timeout=10,
        )
        assert blocked_save.status_code == 409
        wrong_host = httpx.get(
            URL + "/v1/hosts/22222222222242228222222222222222/setup-operations/" + operation_id,
            timeout=10,
        )
        assert wrong_host.status_code == 404
        terminal.locator(".xterm-helper-textarea").focus()
        page.keyboard.type("fixture-input-once")
        page.keyboard.press("Enter")
        page.wait_for_function("window.fixtureOutput.includes('FIXTURE INPUT RECEIVED')")
        wait_for(lambda: any(row.get("value") == "fixture-input-once" for row in rows()[before:]))
        page.evaluate("window.fixtureSockets.at(-1).close(1000,'fixture detach')")
        expect(terminal.get_by_role("button", name="Retry terminal")).to_be_visible(timeout=10000)
        page.screenshot(path=str(RECORD / "detached.png"))
        page.evaluate("window.fixtureOutput=''")
        terminal.get_by_role("button", name="Retry terminal").click()
        page.wait_for_function(
            "window.fixtureOutput.includes('Press Enter to finish:')", timeout=20000
        )
        assert sum(row.get("event") == "input" for row in rows()[before:]) == 1
        assert not any(row.get("event") == "completed" for row in rows()[before:])
        assert_terminal_text_painted(terminal)
        page.screenshot(path=str(RECORD / "reattached.png"))
        recovered_operation = terminal.get_attribute("data-operation-id")
        page.reload()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-id", recovered_operation, timeout=20000)
        expect(terminal).to_have_attribute("data-operation-state", "running", timeout=20000)
        page.wait_for_function(
            "window.fixtureOutput.includes('Press Enter to finish:')", timeout=20000
        )
        assert_terminal_text_painted(terminal)
        assert sum(row.get("event") == "input" for row in rows()[before:]) == 1
        assert not any(row.get("event") == "completed" for row in rows()[before:])
        page.wait_for_timeout(500)
        page.screenshot(path=str(RECORD / "reload-reattached.png"))
        terminal.locator(".xterm-helper-textarea").focus()
        page.keyboard.press("Enter")
        expect(terminal).to_have_attribute("data-operation-state", "succeeded", timeout=20000)
        inventory = httpx.get(
            URL + "/v1/hosts/11111111111141118111111111111111/setup", timeout=10
        ).json()
        assert any(row["kind"] == "subscription" for row in inventory["providers"])
        page.screenshot(path=str(RECORD / "completed.png"))
        other_before = len(rows())
        other_operation = httpx.post(
            URL + "/v1/hosts/22222222222242228222222222222222/setup-operations",
            json={"action": "codex-login"},
            timeout=10,
        ).json()
        assert other_operation["state"] == "running"
        wait_for(lambda: len(rows()) > other_before)
        other_pid = rows()[other_before]["pid"]
        sockets = [
            json.loads(line)
            for line in (STATE / "terminal-fixture-sockets.jsonl").read_text().splitlines()
        ]
        other_socket = next(
            row["socket"]
            for row in sockets
            if row["operation_id"] == other_operation["operation_id"]
        )
        before = len(rows())
        page.get_by_role("button", name="ChatGPT subscription", exact=True).click()
        expect(terminal).to_have_attribute("data-operation-state", "running", timeout=20000)
        wait_for(lambda: len(rows()) > before)
        operation = terminal.get_attribute("data-operation-id")
        sockets = [
            json.loads(line)
            for line in (STATE / "terminal-fixture-sockets.jsonl").read_text().splitlines()
        ]
        owned = next(row for row in sockets if row["operation_id"] == operation)
        pid = rows()[before]["pid"]
        terminal.get_by_role("button", name="Cancel", exact=True).click()
        expect(terminal).to_have_attribute("data-operation-state", "cancelled", timeout=20000)
        wait_for(lambda: not Path(owned["socket"]).exists())

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        wait_for(gone)
        assert Path(other_socket).exists()
        os.kill(other_pid, 0)
        page.screenshot(path=str(RECORD / "cancelled.png"))
        private_markers = (b"fixture-input-once", b"Device code: TEST-1234")
        privacy_files = [*STATE.rglob("*.log"), *STATE.glob("server/data/test.db*")]
        assert privacy_files
        for path in privacy_files:
            contents = path.read_bytes()
            assert not any(marker in contents for marker in private_markers), path
        proof = {
            "result": "passed",
            "pre_attach_prompt": True,
            "detach_reattach_no_input_replay": True,
            "page_reload_recovers_same_operation_without_input_replay": True,
            "subscription_persisted": True,
            "cancel_removed_socket": True,
            "cancel_removed_child": True,
            "other_host_socket_and_child_survived": True,
            "arbitrary_execution_requests_rejected": rejected_requests,
            "concurrent_operation_status": conflict.status_code,
            "save_during_operation_status": blocked_save.status_code,
            "other_host_cannot_observe_operation_status": wrong_host.status_code,
            "terminal_markers_absent_from_application_logs_and_database": [
                str(path.relative_to(STATE)) for path in privacy_files
            ],
            "dummy_only": True,
            "operations": delayed_starts,
        }
        (RECORD / "proof.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps(proof), flush=True)
    except BaseException:
        page.screenshot(path=str(RECORD / "failure.png"), full_page=True)
        print(page.locator("body").inner_text(), flush=True)
        raise
    finally:
        for operation in delayed_starts:
            httpx.delete(
                URL
                + "/v1/hosts/11111111111141118111111111111111/setup-operations/"
                + operation["operation_id"],
                timeout=10,
            )
        if other_operation:
            httpx.delete(
                URL
                + "/v1/hosts/22222222222242228222222222222222/setup-operations/"
                + other_operation["operation_id"],
                timeout=10,
            )
        page.unroute("**/setup-operations", delay_start)
