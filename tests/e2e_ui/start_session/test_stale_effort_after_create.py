r"""UI journey: an in-session effort pick must not replay onto the next created session.

A user changes the reasoning-effort picker *inside* an existing claude-native
session, then creates a brand-new Claude Code session from the new-chat
composer with its effort left at Default. The created session must come up
with no effort override — only choices made in the create composer may
configure it — and the initial prompt must be delivered into its terminal.

While the bug lives, the in-session pick is saved as an app-global sticky
preference and silently re-applied to the newly created session right after
creation: its persisted ``reasoning_effort`` flips from unset to the stale
level even though the create composer showed Default, and the claude-native
launch picks the override up (the pane greets with "... with medium"; when the
pane is already up the override arrives as an unrequested ``/effort`` command
instead). The stays-unset assertion below fails exactly there.

The rig drives the REAL create path end to end: a real ``omnigent host``
process registers this machine against the spawned server, so the landing
composer offers the host and the create POST launches a real host-side runner
and Claude Code terminal. Assertions read the server's session rows and the
created session's tmux pane, so the journey needs no model backend to settle.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Host registration + landing refresh with the discovered host/agents.
_HOST_ONLINE_TIMEOUT_S = 60.0
# Real create -> host launches a runner -> claude terminal boots and the
# bridge types the seed prompt into the pane.
_SEED_IN_PANE_TIMEOUT_S = 120.0
# How long the created session is watched for a late effort rewrite. The buggy
# replay lands before/at bind — well before the pane settles — so this window
# only pads the healthy path.
_EFFORT_SETTLE_S = 10.0
# The stale level picked in the existing session; any non-default level works.
_STALE_EFFORT = "medium"

_EFFORT_PILL = '[data-testid="composer-agent-effort-value"]'


@pytest.fixture
def local_host(live_server: str, tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    """A real ``omnigent host`` registering this machine on the live server.

    The landing composer only enables agent selection and session creation
    once a host is online, so the create journey needs one — exactly like a
    user who ran ``omnigent host`` on their machine.

    :param live_server: Spawned server fixture; the host connects to it.
    :param tmp_path: Holds the host's log for post-mortem on boot failure.
    :returns: The running host process (terminated on teardown).
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    log_path = tmp_path / "host.log"
    with open(log_path, "w") as handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    deadline = time.time() + _HOST_ONLINE_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"omnigent host exited with {proc.returncode}: {log_path.read_text()[-2000:]}"
            )
        resp = httpx.get(f"{live_server}/v1/hosts", timeout=5.0)
        if resp.status_code == 200 and resp.json().get("hosts"):
            break
        time.sleep(1.0)
    else:
        proc.terminate()
        raise RuntimeError(f"host never registered: {log_path.read_text()[-2000:]}")
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _session_json(base_url: str, session_id: str) -> dict[str, object]:
    """Return the session row from ``GET /v1/sessions/{id}``.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The decoded session JSON.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _session_effort(base_url: str, session_id: str) -> str | None:
    """Return the session's persisted ``reasoning_effort`` (``None`` = unset).

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The persisted effort level, or ``None``.
    """
    effort = _session_json(base_url, session_id).get("reasoning_effort")
    return effort if isinstance(effort, str) else None


def _wait_for_session_effort(
    base_url: str, session_id: str, expected: str, *, timeout_s: float = 30.0
) -> None:
    """Wait until the session row persists *expected* as its reasoning effort.

    Confirms the in-session picker change actually landed server-side before
    the journey moves on — a timeout here is a picker-driving problem, not the
    replay bug under test.

    :param base_url: Spawned server base URL.
    :param session_id: The session whose row is polled.
    :param expected: The effort level the row must reach.
    :raises AssertionError: If the row never persists *expected*.
    """
    deadline = time.time() + timeout_s
    last: str | None = None
    while time.time() < deadline:
        last = _session_effort(base_url, session_id)
        if last == expected:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"session {session_id} never persisted reasoning_effort={expected!r} "
        f"after the in-session pick (last seen: {last!r})"
    )


def _pane_text(base_url: str, session_id: str) -> str:
    """Capture the session's claude terminal pane via its tmux socket.

    The host-side runner publishes the pane's tmux socket + target in the
    session's ``terminal`` resource metadata; the host runs on this machine,
    so the pane is directly capturable — the same text a user sees in the
    session's Terminal view.

    :param base_url: Spawned server base URL.
    :param session_id: The session whose claude pane to read.
    :returns: The pane text, or ``""`` while the terminal resource is absent.
    """
    for item in _session_json(base_url, session_id).get("items", []):
        if not (isinstance(item, dict) and item.get("type") == "resource_event"):
            continue
        resource = item.get("data", {}).get("resource", {})
        if resource.get("type") != "terminal":
            continue
        meta = resource.get("metadata", {})
        sock, target = meta.get("tmux_socket"), meta.get("tmux_target")
        if not (sock and target):
            continue
        out = subprocess.run(
            ["tmux", "-S", str(sock), "capture-pane", "-p", "-t", str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout
    return ""


def _select_chat_view(page: Page) -> None:
    """Put the open native session on its Chat view.

    :param page: The Playwright page, on a session surface.
    """
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=120_000)
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _pick_effort_in_session(page: Page, level: str) -> None:
    """Change the open session's reasoning effort via the composer gear.

    :param page: The Playwright page, on the session's chat surface.
    :param level: The effort level value to select, e.g. ``"medium"``.
    """
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=60_000)
    expect(gear).to_be_enabled(timeout=60_000)
    gear.click()
    config_row = page.get_by_test_id("composer-agent-edit")
    expect(config_row).to_be_visible(timeout=30_000)
    config_row.click()
    option = page.get_by_test_id(f"composer-agent-effort-{level}")
    expect(option).to_be_visible(timeout=30_000)
    option.click()
    page.keyboard.press("Escape")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_created_session_keeps_default_effort_after_in_session_pick(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    local_host: subprocess.Popen[str],
) -> None:
    """A new session created with Default effort must not inherit a stale pick.

    Journey: in claude-native session B change the Effort picker to Medium →
    open the new-chat composer, keep effort at Default, type an initial prompt,
    create the session → the created session keeps ``reasoning_effort`` unset,
    its composer shows no effort override, and the initial prompt reaches its
    terminal. Fails while the stale in-session pick is replayed onto the
    created session right after creation.
    """
    base_url, session_b = native_claude_mock_session
    marker = f"seedmark-{os.urandom(4).hex()}"

    page.goto(f"{base_url}/c/{session_b}")
    _select_chat_view(page)
    _pick_effort_in_session(page, _STALE_EFFORT)
    _wait_for_session_effort(base_url, session_b, _STALE_EFFORT)
    _log.info("session B=%s picked effort=%s in-session", session_b, _STALE_EFFORT)

    page.get_by_test_id("new-chat-button").click()
    landing_input = page.get_by_test_id("new-chat-landing-input")
    expect(landing_input).to_be_visible(timeout=30_000)
    agent_picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(agent_picker).to_be_enabled(timeout=60_000)
    agent_picker.click()
    # The host's Claude Code harness row (session-scoped agents don't list).
    claude_row = page.get_by_role("menuitem", name=re.compile(r"^Claude Code"))
    expect(claude_row.first).to_be_visible(timeout=30_000)
    claude_row.first.get_by_text("Edit", exact=True).click()
    expect(agent_picker).to_have_attribute("aria-label", re.compile("Claude Code"), timeout=30_000)
    selected_model = page.get_by_test_id("new-chat-landing-agent-models").locator(
        '[role="menuitemcheckbox"][aria-checked="true"]'
    )
    effort_default = page.get_by_test_id("new-chat-landing-agent-effort-default")
    expect(selected_model).to_have_count(1, timeout=30_000)
    expect(selected_model).to_be_visible()
    expect(effort_default).to_be_visible(timeout=30_000)
    expect(effort_default).to_have_attribute("aria-checked", "true")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    _log.info("landing composer: Claude Code selected, model + effort left at Default")

    landing_input.fill(f"Context marker {marker}. Summarize this repository's README.")
    submit = page.get_by_test_id("new-chat-landing-submit")
    expect(submit).to_be_enabled(timeout=30_000)
    submit.click()

    # The SPA lands on a temp: id first, then swaps in the server's id.
    page.wait_for_url(
        lambda url: "/c/" in url and session_b not in url and "temp:" not in url,
        timeout=90_000,
    )
    created_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]
    _log.info("created session: %s", created_id)

    try:
        _select_chat_view(page)
        # Journey completeness: the seed prompt reaches the created session's
        # real claude pane (needs no model backend — the bridge types it in).
        deadline = time.time() + _SEED_IN_PANE_TIMEOUT_S
        pane = ""
        while time.time() < deadline:
            pane = _pane_text(base_url, created_id)
            if marker in pane:
                break
            time.sleep(2.0)
        assert marker in pane, (
            f"initial prompt never reached the created session's terminal; "
            f"pane tail: {pane[-800:]!r}"
        )
        _log.info("seed prompt delivered into the created session's pane")

        # Leave the composer gear open briefly so the recorded journey shows
        # the effort row the user would check.
        gear = page.get_by_test_id("composer-config-gear")
        if gear.count() and gear.is_enabled():
            gear.click()
            page.wait_for_timeout(2_500)
            page.keyboard.press("Escape")

        # The reported failure: the created session's persisted effort is
        # rewritten to the stale in-session pick right after creation, even
        # though the create composer showed Default.
        deadline = time.time() + _EFFORT_SETTLE_S
        while time.time() < deadline:
            leaked = _session_effort(base_url, created_id)
            if leaked is not None:
                pill = page.locator(_EFFORT_PILL)
                pill_text = pill.inner_text() if pill.count() else "(no effort pill)"
                raise AssertionError(
                    f"created session {created_id} had its reasoning effort silently "
                    f"set to {leaked!r} after creation (composer effort pill shows "
                    f"{pill_text!r}): the previous session's in-session pick was "
                    "replayed onto it although the create composer showed Default"
                )
            time.sleep(0.5)

        # The composer must not claim the stale level either. The pill renders
        # only when a session effort is set, so the healthy path has no pill.
        pill = page.locator(_EFFORT_PILL)
        if pill.count():
            expect(pill).not_to_have_text(_STALE_EFFORT.capitalize(), timeout=5_000)

        # The in-session pick stays scoped to the session it was made in.
        assert _session_effort(base_url, session_b) == _STALE_EFFORT
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{created_id}", timeout=10.0)
