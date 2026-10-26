"""E2E: a send that lost the network mid-flight stops showing a failure once it lands.

A phone can drop off the network while the server holds its send POST, here while
the session's runner relaunches on its host. The browser sees the POST fail and
shows "Something went wrong", but the server still delivers the message. Once the
phone reconnects and the transcript recovers the delivered turn, the failure card
must go too. Desktop Chromium at a phone profile stands in for the native WebView,
and a TCP relay between it and the server stands in for the phone's network.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host
from tests._helpers.polling import poll_until
from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.proxy import FaultProxy, LoopThread, client_link
from tests.e2e_ui.conftest import _register_extra_agent, configure_mock_llm

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
)

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_PROMPT = "offline-send-sentinel summarize the overnight alerts"
_REPLY = "overnight-alert-summary-reply"

_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")


def _launches(host_log: Path) -> list[tuple[str, int]]:
    """Every runner the host daemon launched, in order, as ``(runner_id, pid)``."""
    return [
        (runner_id, int(pid))
        for runner_id, pid in _LAUNCH_LINE.findall(host_log.read_text(errors="replace"))
    ]


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    """Return the server's ``online`` verdict for a runner tunnel."""
    response = client.get(f"/v1/runners/{runner_id}/status", timeout=5.0)
    return response.status_code == 200 and response.json().get("online") is True


class _LaunchHold:
    """SIGSTOP every runner the host launches after ``skip`` launches, until released."""

    def __init__(self, host_log: Path, *, skip: int) -> None:
        self._host_log = host_log
        self._skip = skip
        self._released = threading.Event()
        self.held: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._released.is_set():
            for runner_id, pid in _launches(self._host_log)[self._skip :]:
                if runner_id not in self.held:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGSTOP)
                    self.held[runner_id] = pid
            time.sleep(0.02)

    def release(self) -> None:
        """Stop holding new launches and let every held runner continue."""
        self._released.set()
        self._thread.join(timeout=5.0)
        for pid in self.held.values():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)


@_PHONE
@pytest.mark.timeout(300)
def test_failure_card_clears_once_reconnect_recovers_the_delivered_send(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The card from a send whose POST died offline clears when its turn reappears."""
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="offline-send-failure-card",
        match=_PROMPT,
    )
    loop = LoopThread("offline-send-failure-card")
    loop.start()
    network = FaultProxy(
        "browser",
        ("127.0.0.1", urlsplit(live_server).port or 80),
        loop=loop,
        events=EventLog(),
        classify=client_link,
    )
    network.start()
    hold: _LaunchHold | None = None
    try:
        with (
            httpx.Client(base_url=live_server) as client,
            isolated_host(
                tmp_path / "host",
                live_server,
                env={
                    # Spawn runners directly, so a relaunch can be held before it connects.
                    "OMNIGENT_RUNNER_ZYGOTE": "0",
                    "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                    "OPENAI_API_KEY": "mock-key",
                },
            ) as host,
        ):
            host.wait_online(client, timeout=45)
            agent_id = _register_extra_agent(
                live_server, "offline-send-agent", "You are a terse smoke-test assistant."
            )
            assert agent_id is not None
            workspace = tmp_path / "project"
            workspace.mkdir()
            created = client.post(
                "/v1/sessions",
                json={"agent_id": agent_id, "host_id": host.host_id, "workspace": str(workspace)},
                timeout=90.0,
            )
            created.raise_for_status()
            session_id = created.json()["id"]

            poll_until(lambda: _launches(host.log_path), timeout_s=30)
            (first_runner, first_pid), *_ = _launches(host.log_path)
            poll_until(lambda: _runner_online(client, first_runner), timeout_s=45)

            page.goto(f"{network.url}/c/{session_id}")
            composer = page.locator(_COMPOSER)
            expect(composer).to_be_visible(timeout=30_000)

            # The session's runner goes away, so the next send relaunches one.
            hold = _LaunchHold(host.log_path, skip=len(_launches(host.log_path)))
            os.kill(first_pid, signal.SIGKILL)
            poll_until(lambda: not _runner_online(client, first_runner), timeout_s=30)

            composer.tap()
            composer.fill(_PROMPT)
            page.get_by_role("button", name="Send", exact=True).tap()

            # The relaunch for this send is held, so the server is still holding its POST.
            poll_until(lambda: hold.held, timeout_s=30, interval_s=0.05)
            held_runner = next(iter(hold.held))
            assert not _runner_online(client, held_runner), (
                f"fault injection lost the race: runner {held_runner} connected before SIGSTOP"
            )

            # The phone drops off the network: its connections reset and new ones fail.
            with network.refuse():
                assert network.reset() > 0, "the browser had no live connection to cut"
                # The POST died without an answer, so the send reads as failed.
                expect(page.get_by_test_id("error-pill")).to_be_visible(timeout=15_000)
                expect(page.get_by_test_id("error-headline")).to_have_text("Something went wrong")
                hold.release()
                # The server delivers the message and the reply while the phone is away.
                poll_until(
                    lambda: _REPLY in client.get(f"/v1/sessions/{session_id}/items").text,
                    timeout_s=60,
                )

            # The reconnect recovers the delivered turn, and nothing still reports
            # the send as failed or hands its text back to the composer.
            expect(page.locator(_USER_BUBBLE).filter(has_text=_PROMPT)).to_be_visible(
                timeout=30_000
            )
            expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=_REPLY)).to_be_visible(
                timeout=30_000
            )
            expect(page.get_by_test_id("error-pill")).to_have_count(0)
            expect(composer).to_have_value("")
    finally:
        if hold is not None:
            hold.release()
            for pid in hold.held.values():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        network.stop()
        loop.stop()
