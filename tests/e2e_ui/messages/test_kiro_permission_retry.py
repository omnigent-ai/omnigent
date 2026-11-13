"""Real browser/server/runner/tmux journeys with lost or delayed fake Kiro input."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.harnesses.kiro_native.bridge import acp_record_path
from omnigent.harnesses.kiro_native.permissions import kiro_permission_elicitation_id
from tests._helpers.polling import poll_until

from .test_kiro_concurrent_permissions import (
    _FAKE_KIRO_SOURCE,
    _pending_elicitations,
    _recorder_response_ids,
)
from .test_kiro_concurrent_permissions import (
    kiro_shim_processes as _kiro_shim_processes,
)
from .test_kiro_concurrent_permissions import (
    kiro_shim_session as _kiro_shim_session,
)
from .test_message_render_parity import _ensure_chat_view, _select_view_mode, _send

kiro_shim_session = _kiro_shim_session
kiro_shim_processes = _kiro_shim_processes


def _shim(mode: str, *, dropped_enters: int = 1, write: bool = False) -> str:
    commands_anchor = 'COMMANDS = ("alpha", "beta", "gamma")'
    enter_anchor = "    def on_enter(self):\n        if self.active is not None:"
    assert _FAKE_KIRO_SOURCE.count(commands_anchor) == 1
    assert _FAKE_KIRO_SOURCE.count(enter_anchor) == 1
    source = _FAKE_KIRO_SOURCE.replace(commands_anchor, 'COMMANDS = ("alpha", "alpha")')
    if write:
        replacements = {
            '"title": "Running: touch /tmp/setup-step-%s" % name,': (
                '"title": "Creating check.txt", "path": "/workspace/%s/check.txt" % index,'
            ),
            'lines.append(self.active["title"])': (
                'lines.extend(["↓ Write " + self.active["path"], '
                '"  added 1 line in check.txt", "     1+  hello from Kiro", SEP])'
            ),
            '"title": request["title"],': '"title": request["title"], "rawInput": '
            '{"command": "create", "path": request["path"], "content": "hello from Kiro"},',
            '"sessionId": "fake-kiro-session",': '"sessionId": "fake-kiro-session", "_meta": '
            '{"trustOptions": [{"setting_key": "runtime_write_paths"}]},',
        }
        for old, new in replacements.items():
            assert source.count(old) == 1
            source = source.replace(old, new)
    return source.replace(
        enter_anchor,
        f"""    def on_enter(self):
        if self.active is not None:
            with open(RECORD_PATH + ".enters", "a") as handle:
                handle.write(self.active["id"] + "\\n")
            self.enter_count = getattr(self, "enter_count", 0) + 1
            if {mode!r} == "drop" and self.enter_count <= {dropped_enters}:
                return
            if {mode!r} == "delay" and self.enter_count == 1:
                import time
                time.sleep(9)
""",
    )


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ("surface", "kiro_shim_session"),
    [(surface, _shim("drop")) for surface in ("chat", "inbox", "standalone", "reject", "terminal")]
    + [(surface, _shim("drop", write=True)) for surface in ("write", "write-reject")],
    indirect=["kiro_shim_session"],
    ids=["chat", "inbox", "standalone", "reject", "terminal", "write", "write-reject"],
)
def test_kiro_manual_retry(
    kiro_shim_session: tuple[str, str, Path], page: Page, surface: str, tmp_path: Path
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    first_id = "perm-req-1-1"
    eid = kiro_permission_elicitation_id(session_id, first_id)
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    card = page.locator('[data-testid="approval-card"][data-state="pending"]').first
    expect(card).to_be_visible(timeout=120_000)
    if surface == "inbox":
        page.goto(f"{base_url}/inbox")
    elif surface == "standalone":
        page.goto(f"{base_url}/approve/{session_id}/{eid}")
    held_responses: list[Route] = []
    resolve_url = f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve"
    if surface in ("inbox", "standalone"):

        def hold_first_response(route: Route) -> None:
            if held_responses:
                route.continue_()
                return
            assert route.fetch().status == 202
            held_responses.append(route)

        page.route(resolve_url, hold_first_response)
    started = time.monotonic()
    reject = surface in ("reject", "write-reject")
    page.get_by_role("button", name="Reject" if reject else "Approve", exact=True).click()
    expect(page.get_by_text("Waiting for Kiro to confirm")).to_be_visible()
    retry = page.get_by_role(
        "button",
        name="Retry sending rejection" if reject else "Retry sending approval",
    )
    expect(retry).not_to_be_visible()
    expect(retry).to_be_visible(timeout=12_000)
    assert time.monotonic() - started >= 5.0
    assert _recorder_response_ids(record_file) == set()
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        first_id
    ]
    if surface not in ("inbox", "standalone"):
        page.reload()
        expect(retry).to_be_visible(timeout=15_000)
    else:
        assert len(held_responses) == 1
    if surface == "chat":
        close_sidebar = page.get_by_role("button", name="Close sidebar", exact=True)
        if close_sidebar.is_visible():
            close_sidebar.click()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(retry).to_be_in_viewport()
    expect(
        page.get_by_text("The previous attempt may still be pending.", exact=False)
    ).to_be_visible()
    page.screenshot(path=str(tmp_path / "delivery-unconfirmed.png"), full_page=True)
    if surface == "terminal":
        _select_view_mode(page, "Terminal")
        terminal_view = page.get_by_test_id("terminal-view").last
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
        terminal_view.locator(".xterm-helper-textarea").focus()
        page.keyboard.press("Enter")
        _select_view_mode(page, "Chat")
    else:
        with page.expect_request(resolve_url):
            retry.click()
        if held_responses:
            held_responses[0].fulfill(status=503, json={"error": "late response failure"})
    poll_until(lambda: first_id in _recorder_response_ids(record_file), timeout_s=15)
    expect(retry).not_to_be_visible(timeout=15_000)
    second_eid = kiro_permission_elicitation_id(session_id, "perm-req-1-2")
    poll_until(
        lambda: second_eid in json.dumps(_pending_elicitations(base_url, session_id)), timeout_s=15
    )
    page.wait_for_timeout(1_000)
    assert _recorder_response_ids(record_file) == {first_id}
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        first_id,
        first_id,
    ]
    responses = [json.loads(line)["msg"] for line in record_file.read_text().splitlines()]
    result = next(
        msg["result"] for msg in responses if msg.get("id") == first_id and "result" in msg
    )
    assert result["outcome"]["optionId"] == ("reject-1-1" if reject else "allow-1-1")


@pytest.mark.timeout(180)
@pytest.mark.parametrize("kiro_shim_session", [_shim("delay")], indirect=True, ids=["late-enter"])
@pytest.mark.parametrize("reconnect", [False, True], ids=["connected", "reconnect"])
def test_kiro_late_confirmation_removes_retry(
    kiro_shim_session: tuple[str, str, Path], page: Page, reconnect: bool
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    approve = page.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_visible(timeout=120_000)
    approve.click()
    retry = page.get_by_role("button", name="Retry sending approval")
    expect(retry).to_be_visible(timeout=12_000)
    if reconnect:
        page.context.set_offline(True)
        try:
            poll_until(lambda: "perm-req-1-1" in _recorder_response_ids(record_file), timeout_s=15)
        finally:
            page.context.set_offline(False)
    expect(retry).not_to_be_visible(timeout=12_000)
    expect(page.get_by_text("Resolved in Kiro")).to_be_visible()
    expect(approve).to_be_visible(timeout=15_000)
    page.wait_for_timeout(1_000)
    assert _recorder_response_ids(record_file) == {"perm-req-1-1"}
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        "perm-req-1-1"
    ]


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "kiro_shim_session",
    [
        {
            "source": _shim("delay"),
            "server_checkout": os.environ.get("OMNIGENT_E2E_KIRO_LEGACY_SERVER_CHECKOUT"),
        }
    ],
    indirect=True,
    ids=["legacy-server-delayed-enter"],
)
def test_kiro_legacy_server_does_not_repeat_enter(
    kiro_shim_session: tuple[str, str, Path], page: Page
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible(timeout=120_000)
    snapshot = _pending_elicitations(base_url, session_id)
    assert "native_delivery" not in snapshot[0]["params"], "requires a pre-delivery-state server"
    page.get_by_role("button", name="Approve", exact=True).click()
    poll_until(lambda: "perm-req-1-1" in _recorder_response_ids(record_file), timeout_s=20)
    page.wait_for_timeout(2_000)
    assert _recorder_response_ids(record_file) == {"perm-req-1-1"}
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        "perm-req-1-1"
    ]
    expect(page.get_by_role("button", name="Retry sending approval")).not_to_be_visible()
    second_eid = kiro_permission_elicitation_id(session_id, "perm-req-1-2")
    assert second_eid in json.dumps(_pending_elicitations(base_url, session_id))


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "kiro_shim_session",
    [_shim("drop", dropped_enters=2)],
    indirect=True,
    ids=["two-lost-enters"],
)
def test_kiro_two_tabs_cannot_repeat_an_old_retry(
    kiro_shim_session: tuple[str, str, Path], page: Page
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    enters_file = record_file.with_suffix(record_file.suffix + ".enters")
    first_id = "perm-req-1-1"
    eid = kiro_permission_elicitation_id(session_id, first_id)
    detail_url = f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}"
    resolve_url = f"{detail_url}/resolve"
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    page.get_by_role("button", name="Approve", exact=True).click(timeout=120_000)
    retry = page.get_by_role("button", name="Retry sending approval")
    expect(retry).to_be_visible(timeout=12_000)

    stale_detail = page.request.get(detail_url).json()
    held_requests: list[Route] = []
    other = page.context.new_page()
    try:
        # Keep the second tab on an old poll response while the server advances.
        other.route(detail_url, lambda route: route.fulfill(json=stale_detail))
        other.goto(f"{base_url}/approve/{session_id}/{eid}")
        other_retry = other.get_by_role("button", name="Retry sending approval")
        expect(other_retry).to_be_visible()
        page.route(resolve_url, lambda route: held_requests.append(route))
        other.route(resolve_url, lambda route: held_requests.append(route))
        with page.expect_request(resolve_url):
            retry.click()
        with other.expect_request(resolve_url):
            other_retry.click()
        deadline = time.monotonic() + 5
        while len(held_requests) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        expect(retry).not_to_be_visible()
        expect(other_retry).not_to_be_visible()
        assert len(held_requests) == 2
        assert held_requests[0].request.post_data_json == held_requests[1].request.post_data_json
        with page.expect_response(resolve_url) as first_response:
            held_requests[0].continue_()
        with other.expect_response(resolve_url) as duplicate_response:
            held_requests[1].continue_()
        assert first_response.value.status == 202
        assert duplicate_response.value.status == 202
        page.unroute(resolve_url)
        other.unroute(resolve_url)
        expect(retry).to_be_enabled(timeout=12_000)
        expect(other_retry).to_be_enabled(timeout=12_000)
        latest_detail = page.request.get(detail_url).json()
        assert (
            latest_detail["native_delivery"]["attempt"]
            != stale_detail["native_delivery"]["attempt"]
        )
        assert enters_file.read_text().splitlines() == [first_id, first_id]

        # The visible stale button must not answer this or the next request.
        with other.expect_response(resolve_url) as stale_response:
            other_retry.click()
        assert stale_response.value.status == 400
        page.wait_for_timeout(1_000)
        assert enters_file.read_text().splitlines() == [first_id, first_id]
        assert _recorder_response_ids(record_file) == set()

        other.unroute(detail_url)
        other.reload()
        expect(other_retry).to_be_visible()
        other_retry.click()
        poll_until(lambda: first_id in _recorder_response_ids(record_file), timeout_s=15)
        expect(retry).not_to_be_visible(timeout=15_000)
        expect(other.get_by_text("Elicitation resolved", exact=True)).to_be_visible(timeout=15_000)
        second_eid = kiro_permission_elicitation_id(session_id, "perm-req-1-2")
        poll_until(
            lambda: second_eid in json.dumps(_pending_elicitations(base_url, session_id)),
            timeout_s=15,
        )
        page.wait_for_timeout(1_000)
        assert _recorder_response_ids(record_file) == {first_id}
        assert enters_file.read_text().splitlines() == [first_id, first_id, first_id]
    finally:
        other.close()


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "kiro_shim_session", [_shim("drop")], indirect=True, ids=["reporting-outage"]
)
def test_kiro_retry_recovers_after_reporting_outage(
    kiro_shim_session: tuple[str, str, Path],
    kiro_shim_processes: dict[str, subprocess.Popen[bytes]],
    page: Page,
) -> None:
    base_url, sid, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    enters_file = record_file.with_suffix(record_file.suffix + ".enters")
    first_id = "perm-req-1-1"
    eid = kiro_permission_elicitation_id(sid, first_id)
    detail_url = f"{base_url}/v1/sessions/{sid}/elicitations/{eid}"
    page.goto(f"{base_url}/c/{sid}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    approve = page.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_visible(timeout=120_000)
    standalone = page.context.new_page()
    inbox = page.context.new_page()
    runner = kiro_shim_processes["runner"]
    try:
        standalone.goto(f"{base_url}/approve/{sid}/{eid}")
        expect(standalone.get_by_role("button", name="Approve", exact=True)).to_be_visible()
        inbox.goto(f"{base_url}/inbox")
        approve.click()
        poll_until(lambda: enters_file.exists(), timeout_s=5)
        runner.send_signal(signal.SIGSTOP)
        started = time.monotonic()
        # Leave the browser and server live for the real 30-second cleanup.
        expect(page.get_by_text("Prompt expired", exact=False)).to_be_visible(timeout=40_000)
        assert time.monotonic() - started >= 28
        expect(standalone.get_by_text("Delivery unconfirmed", exact=True)).to_be_visible()
        assert page.request.get(detail_url).json() == {
            "status": "resolved",
            "resolution": "unanswered",
        }
        assert _recorder_response_ids(record_file) == set()
        assert enters_file.read_text().splitlines() == [first_id]
        runner.send_signal(signal.SIGCONT)
        for surface in (page, standalone, inbox):
            expect(surface.get_by_role("button", name="Retry sending approval")).to_be_visible(
                timeout=20_000
            )
        assert page.request.get(detail_url).json()["native_delivery"]["state"] == "unconfirmed"
        assert enters_file.read_text().splitlines() == [first_id]
        standalone.get_by_role("button", name="Retry sending approval").click()
        poll_until(lambda: first_id in _recorder_response_ids(record_file), timeout_s=15)
        expect(page.get_by_text("Resolved in Kiro", exact=False)).to_be_visible()
        expect(standalone.get_by_text("Elicitation resolved", exact=True)).to_be_visible()
        for surface in (page, standalone, inbox):
            expect(
                surface.get_by_role("button", name="Retry sending approval")
            ).not_to_be_visible()
        assert enters_file.read_text().splitlines() == [first_id, first_id]
    finally:
        if runner.poll() is None:
            runner.send_signal(signal.SIGCONT)
        standalone.close()
        inbox.close()
