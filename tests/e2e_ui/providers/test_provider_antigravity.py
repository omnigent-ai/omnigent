"""Guarded real-host Antigravity Connect journey using only a disposable agy CLI."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests._helpers.provider_setup_terminal import ProviderSetupTerminalRuntime
from tests.e2e_ui.providers.test_provider_guided import assert_terminal_text_painted

HOST_ID = "11111111111141118111111111111111"


def _wait_for(predicate, timeout: float = 20) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Disposable Antigravity fixture condition timed out")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _select_antigravity(page: Page) -> None:
    page.get_by_role("button", name="More agents").click()
    page.get_by_test_id("setup-agent-antigravity").click()


def _socket_for(runtime: ProviderSetupTerminalRuntime, operation_id: str) -> Path:
    rows = _rows(runtime.root / "terminal-fixture-sockets.jsonl")
    return Path(next(row["socket"] for row in rows if row["operation_id"] == operation_id))


def _child_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def exercise_antigravity(
    page: Page, runtime: ProviderSetupTerminalRuntime, recordings: Path
) -> None:
    root = runtime.root
    log = root / "terminal-fixture-inputs.jsonl"
    signed_in = root / "agy-signed-in"
    api = f"{runtime.url}/v1/hosts/{HOST_ID}/setup-operations"
    recordings.mkdir(parents=True, exist_ok=True)
    operations: list[str] = []
    config = root / "host-a/config/config.yaml"
    original_config = config.read_bytes()
    try:
        signed_in.write_text("fixture only\n")
        page.goto(runtime.url + "/settings/providers")
        page.get_by_test_id("settings-providers-host").click()
        page.get_by_role("option", name="Fixture computer A · online", exact=True).click()
        _select_antigravity(page)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/setup-operations")
            )
        ) as preflight_response:
            page.get_by_role("button", name="Sign in to Antigravity").click()
        preflight = preflight_response.value.json()
        assert preflight["state"] == "succeeded" and preflight["already_connected"] is True
        expect(page.get_by_text("Antigravity is already signed in", exact=False)).to_be_visible()
        expect(page.get_by_role("region", name="Provider setup terminal")).to_have_count(0)
        assert not _rows(log) and not _rows(root / "terminal-fixture-sockets.jsonl")
        assert config.read_bytes() == original_config
        page.screenshot(path=str(recordings / "preflight-already-signed-in.png"), full_page=True)

        signed_in.unlink()
        page.reload()
        _select_antigravity(page)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/setup-operations")
            )
        ) as start_response:
            page.get_by_role("button", name="Sign in to Antigravity").click()
        started = start_response.value.json()
        assert started["state"] == "running" and started["can_verify"] is True
        operation_id = started["operation_id"]
        operations.append(operation_id)
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-id", operation_id)
        expect(terminal).to_have_attribute("data-operation-state", "running")
        expect(terminal.get_by_role("button", name="Check connection")).to_be_visible()
        _wait_for(lambda: any(row["event"] == "agy_prompt_written" for row in _rows(log)))
        pid = next(row["pid"] for row in _rows(log) if row["event"] == "agy_started")
        socket = _socket_for(runtime, operation_id)
        assert socket.exists() and _child_alive(pid)
        assert_terminal_text_painted(terminal)
        page.screenshot(path=str(recordings / "interactive-running.png"), full_page=True)

        refused = httpx.post(f"{api}/{operation_id}/verify", timeout=10)
        assert refused.status_code == 409, refused.text
        still_running = httpx.get(f"{api}/{operation_id}", timeout=10).json()
        assert still_running["state"] == "running" and still_running["can_verify"] is True
        assert socket.exists() and _child_alive(pid)
        terminal.get_by_role("button", name="Check connection").click()
        expect(terminal).to_contain_text("Connection not detected yet")
        expect(terminal).to_have_attribute("data-operation-state", "running")

        page.get_by_role("button", name="Back to agents").click()
        expect(page.get_by_text("Antigravity connection in progress.")).to_be_visible()
        page.get_by_test_id("setup-agent-codex").click()
        expect(page.get_by_role("button", name="Return to Antigravity")).to_be_visible()
        expect(
            page.get_by_role("button", name="ChatGPT subscription", exact=True)
        ).to_be_disabled()
        page.screenshot(path=str(recordings / "active-operation-navigation.png"), full_page=True)
        page.get_by_role("button", name="Return to Antigravity").click()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-id", operation_id)
        page.reload()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-id", operation_id, timeout=20000)
        expect(terminal).to_have_attribute("data-operation-state", "running")
        assert socket.exists() and _child_alive(pid)
        page.screenshot(path=str(recordings / "reload-reattached.png"), full_page=True)

        terminal.locator(".xterm-helper-textarea").focus()
        page.keyboard.type("fixture-signin")
        page.keyboard.press("Enter")
        _wait_for(signed_in.exists)
        assert _child_alive(pid) and socket.exists()
        terminal.get_by_role("button", name="Check connection").click()
        expect(terminal).to_have_attribute("data-operation-state", "succeeded", timeout=20000)
        _wait_for(lambda: not socket.exists() and not _child_alive(pid))
        assert _rows(log)[-1]["value"] == "fixture-signin"
        page.screenshot(path=str(recordings / "verified.png"), full_page=True)
        terminal.get_by_role("button", name="View output", exact=True).click()
        assert_terminal_text_painted(terminal)
        page.screenshot(path=str(recordings / "retained-output.png"), full_page=True)
        page.get_by_role("button", name="Back to agents").click()
        page.get_by_test_id("setup-agent-codex").click()
        expect(page.get_by_role("button", name="ChatGPT subscription", exact=True)).to_be_enabled()
        expect(page.get_by_role("button", name="Return to Antigravity")).to_have_count(0)
        assert config.read_bytes() == original_config

        page.reload()
        _select_antigravity(page)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/setup-operations")
            )
        ) as second_preflight_response:
            page.get_by_role("button", name="Sign in to Antigravity").click()
        second_preflight = second_preflight_response.value.json()
        assert second_preflight["state"] == "succeeded"
        assert second_preflight["already_connected"] is True
        expect(page.get_by_role("region", name="Provider setup terminal")).to_have_count(0)

        signed_in.unlink()
        page.reload()
        _select_antigravity(page)
        page.get_by_role("button", name="Sign in to Antigravity").click()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        expect(terminal).to_have_attribute("data-operation-state", "running", timeout=20000)
        cancelled_id = terminal.get_attribute("data-operation-id")
        assert cancelled_id
        operations.append(cancelled_id)
        cancelled_socket = _socket_for(runtime, cancelled_id)
        _wait_for(
            lambda: any(
                row["event"] == "agy_prompt_written" and row["pid"] != pid for row in _rows(log)
            )
        )
        cancelled_pid = next(
            row["pid"] for row in reversed(_rows(log)) if row["event"] == "agy_started"
        )
        page.get_by_role("button", name="Back to agents").click()
        page.get_by_role("button", name="Return to Antigravity").click()
        terminal = page.get_by_role("region", name="Provider setup terminal")
        terminal.get_by_role("button", name="Cancel", exact=True).click()
        expect(terminal).to_have_attribute("data-operation-state", "cancelled", timeout=20000)
        _wait_for(lambda: not cancelled_socket.exists() and not _child_alive(cancelled_pid))
        page.screenshot(path=str(recordings / "cancelled.png"), full_page=True)

        proof = {
            "result": "passed",
            "dummy_only": True,
            "preflight_already_connected_without_terminal": True,
            "configuration_unchanged": True,
            "rendered_prompt_and_retained_output_visible": True,
            "conflicting_controls_disabled_then_reenabled": True,
            "signed_out_interactive_cli": True,
            "false_verify_status": refused.status_code,
            "false_verify_kept_operation_running": True,
            "input_marked_signed_in_while_child_alive": True,
            "true_verify_succeeded_and_closed_terminal": True,
            "navigation_banner_returned_to_same_operation": True,
            "reload_reattached_same_operation": True,
            "subsequent_preflight_without_terminal": True,
            "cancel_closed_terminal_and_child": True,
            "operations": operations,
        }
        (recordings / "proof.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps(proof), flush=True)
    except BaseException:
        page.screenshot(path=str(recordings / "failure.png"), full_page=True)
        raise
    finally:
        for operation_id in operations:
            httpx.delete(f"{api}/{operation_id}", timeout=10)
