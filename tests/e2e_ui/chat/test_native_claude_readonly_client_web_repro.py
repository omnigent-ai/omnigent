r"""UI journey: web-UI **chat** input fails on ``claude-native`` when
the session's only attached tmux client is a read-only control-mode viewer.

The ``claude-native`` ("Claude Code") wrapper runs a real ``claude`` CLI in the
session's private tmux, and forwards web-composer messages INTO it by shelling
out ``tmux send-keys`` against that socket (see
``omnigent/harnesses/claude_native/bridge.py``). From tmux 3.5 onward
``send-keys`` is **refused** ("client is read-only", rc=1) whenever the session's
sole attached client is a read-only (``-r``) control-mode client -- the exact
client the SPA's Terminal view attaches for a viewer who lacks write access
(``omnigent/terminals/control_bridge.py`` adds ``-r``; ``load-buffer`` /
``paste-buffer`` are unaffected, so the pane keeps rendering). The runner's inject
then raises, surfacing in the web Chat view as an ``error-pill`` reading
``inner executor error: tmux command failed (rc=1): client is read-only``.

This journey drives the real product path: it boots the runner's Claude Code TUI
(against the in-process mock Anthropic gateway, like the render-parity suite),
stays in the **Chat** view so the owner never attaches a writable client, then
attaches a read-only control client out-of-band with the *exact argv the product
uses for a read-only viewer* -- faithfully recreating "the session's only attached
tmux client is a read-only viewer" without depending on the reporter's
non-deterministic stuck-write-role trigger. It then sends from the web composer
and asserts the turn is delivered with no executor error.

The assertion is written for a HEALTHY build (no ``error-pill``): on the current
build it fails because the pill renders, and the browser recording captures that
failure; once the bridge stops driving ``send-keys`` against a read-only-only
client, it passes -- a fail->pass regression guard.

Requires tmux >= 3.5 on PATH (or ``OMNIGENT_E2E_TMUX``); the refusal does not
exist on older tmux, so the journey skips there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_log = logging.getLogger(__name__)

# The web Chat view renders a turn's executor failure as this pill; its expanded
# body carries the raw message ("inner executor error: tmux command failed ...").
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_BODY = '[data-testid="error-message-content"]'
# The composer's stable aria-label (its placeholder mutates with state).
_COMPOSER_LABEL = "Message the agent"
# Claude Code frames its input box with this prompt glyph; its presence in the
# pane means the TUI is ready for a message -- the state the runner injects into.
_PROMPT_GLYPH = "❯"  # ❯

# claude-native auto-launch + first-run pre-accept can take a while under load.
_TUI_READY_TIMEOUT_S = 150.0
# How long to spend forcing the sole-read-only-client precondition.
_CLIENT_STATE_TIMEOUT_S = 30.0
# How long the failure (or, post-fix, the settled turn) is given to render.
_OUTCOME_SETTLE_MS = 5_000

_TMUX_ADVERT_FILE = "tmux.json"


def _resolve_tmux() -> str:
    """Resolve the tmux binary the runner and this test share.

    :returns: ``OMNIGENT_E2E_TMUX`` if set, else the first ``tmux`` on PATH.
    """
    return os.environ.get("OMNIGENT_E2E_TMUX") or shutil.which("tmux") or "tmux"


def _tmux_version(tmux: str) -> tuple[int, ...]:
    """Return the ``(major, minor)`` version of *tmux*, or ``(0, 0)``.

    :param tmux: Path to the tmux binary.
    :returns: The parsed version tuple.
    """
    out = subprocess.run(
        [tmux, "-V"], capture_output=True, text=True, check=False, timeout=10
    ).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _tmux_advert(base_url: str, session_id: str) -> tuple[str, str] | None:
    """Resolve the session terminal's private tmux socket + pane target.

    Mirrors the render-parity suite: read the session's bridge-id label, then the
    ``tmux.json`` the runner advertises in that bridge directory.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: ``(socket_path, tmux_target)`` once advertised, else ``None``.
    """
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    labels = session.get("labels") or {}
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
    advert = bridge_dir_for_bridge_id(bridge_id) / _TMUX_ADVERT_FILE
    if not advert.exists():
        return None
    info = json.loads(advert.read_text(encoding="utf-8"))
    return info["socket_path"], info["tmux_target"]


def _capture_pane(tmux: str, socket_path: str, target: str) -> str:
    """Capture the TUI's tmux pane text (``capture-pane`` works read-only).

    :param tmux: tmux binary path.
    :param socket_path: The private tmux socket.
    :param target: The pane target string.
    :returns: The pane's visible text, or ``""`` on failure.
    """
    proc = subprocess.run(
        [tmux, "-S", socket_path, "capture-pane", "-t", target, "-p"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _list_clients(tmux: str, socket_path: str) -> list[tuple[str, str]]:
    """List attached clients as ``(name, flags)`` pairs.

    :param tmux: tmux binary path.
    :param socket_path: The private tmux socket.
    :returns: One ``(client_name, client_flags)`` pair per attached client.
    """
    proc = subprocess.run(
        [tmux, "-S", socket_path, "list-clients", "-F", "#{client_name}\t#{client_flags}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        return []
    pairs: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, _, flags = line.partition("\t")
        pairs.append((name, flags))
    return pairs


def _detach_writable_clients(tmux: str, socket_path: str) -> None:
    """Detach any client that is NOT read-only, leaving read-only viewers.

    The SPA owner would attach a writable client; dropping it lets the injected
    ``send-keys`` see the read-only viewer as the session's *sole* client -- the
    reported precondition.

    :param tmux: tmux binary path.
    :param socket_path: The private tmux socket.
    """
    for name, flags in _list_clients(tmux, socket_path):
        if "read-only" not in flags:
            subprocess.run(
                [tmux, "-S", socket_path, "detach-client", "-t", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )


@pytest.mark.nightly
@pytest.mark.timeout(400)
def test_native_claude_web_chat_delivers_with_sole_readonly_viewer(
    browser: Browser,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Web chat delivers on ``claude-native`` even when the sole tmux client is read-only.

    Red on the current build (the composer send raises "tmux command failed
    (rc=1): client is read-only" and an ``error-pill`` renders); green once the
    bridge no longer drives ``send-keys`` against a read-only-only client.
    """
    tmux = _resolve_tmux()
    version = _tmux_version(tmux)
    if version < (3, 5):
        pytest.skip(
            f"tmux {version or 'unknown'} does not refuse send-keys for a read-only "
            "client (behaviour introduced in 3.5); this journey cannot be exhibited"
        )

    base_url, session_id = native_claude_mock_session
    _log.info(
        "readonly-viewer web repro: base_url=%s session_id=%s tmux=%s",
        base_url,
        session_id,
        version,
    )

    # Film the journey when the recording lane requests it. The conftest's
    # OMNIGENT_E2E_RECORD_DIR injection only covers the async Browser API,
    # and this test drives the sync ``browser`` fixture.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context = browser.new_context(
        viewport={"width": 1360, "height": 900},
        record_video_dir=record_dir or None,
        record_video_size={"width": 1360, "height": 900} if record_dir else None,
        ignore_https_errors=True,
    )
    page = context.new_page()
    viewer: subprocess.Popen[bytes] | None = None
    try:
        page.goto(f"{base_url}/c/{session_id}")
        # Stay in Chat view so the owner never attaches a WRITABLE control client
        # (which would let send-keys through and mask the bug).
        _ensure_chat_view(page)
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(
            timeout=int(_TUI_READY_TIMEOUT_S * 1000)
        )

        # Wait until the runner has advertised the terminal's tmux socket AND the
        # Claude TUI has rendered its framed prompt -- the state a message injects
        # into. Injecting before this would time out on a different (readiness)
        # error, not the read-only refusal under test.
        deadline = time.monotonic() + _TUI_READY_TIMEOUT_S
        advert: tuple[str, str] | None = None
        while time.monotonic() < deadline:
            advert = _tmux_advert(base_url, session_id)
            if advert and _PROMPT_GLYPH in _capture_pane(tmux, advert[0], advert[1]):
                break
            time.sleep(1.0)
        assert advert, "claude-native never advertised its tmux socket (tmux.json)"
        socket_path, target = advert
        assert _PROMPT_GLYPH in _capture_pane(tmux, socket_path, target), (
            "Claude Code TUI never rendered its prompt; cannot exercise the inject path"
        )

        # Attach a read-only control client with the EXACT argv the product uses
        # for a read-only viewer (control_bridge.attach_control_client):
        #   tmux -S <sock> -f /dev/null -C attach -r
        viewer = subprocess.Popen(
            [tmux, "-S", socket_path, "-f", "/dev/null", "-C", "attach", "-r"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Make that read-only viewer the SOLE attached client: drop any writable
        # client, then confirm every remaining client is read-only.
        state_deadline = time.monotonic() + _CLIENT_STATE_TIMEOUT_S
        clients: list[tuple[str, str]] = []
        while time.monotonic() < state_deadline:
            _detach_writable_clients(tmux, socket_path)
            clients = _list_clients(tmux, socket_path)
            if clients and all("read-only" in flags for _, flags in clients):
                break
            time.sleep(0.5)
        _log.info("tmux clients before send: %s", clients)
        assert clients and all("read-only" in flags for _, flags in clients), (
            f"could not establish a sole read-only client; clients={clients}"
        )

        # Send from the web composer -> runner inject -> tmux send-keys, refused
        # by tmux >=3.5 while the only client is read-only.
        _detach_writable_clients(tmux, socket_path)
        _send(page, "chat send while the only tmux client is a read-only viewer")

        # Give the outcome time to render on the recording. On the current build
        # an error-pill carrying the tmux read-only refusal appears here; expand
        # it so the recording shows the raw executor message.
        page.wait_for_timeout(_OUTCOME_SETTLE_MS)
        pill = page.locator(_ERROR_PILL)
        if pill.count():
            pill.first.click()  # expand the disclosure to reveal the message
            page.wait_for_timeout(1_500)
            body = page.locator(_ERROR_BODY).first
            detail = body.inner_text() if body.count() else pill.first.inner_text()
            _log.info("readonly-viewer failure reproduced: error detail=%r", detail)
            page.wait_for_timeout(2_000)  # let the expanded failure sit on the recording

        # Durable assertion: a healthy build delivers the turn with no executor
        # error. Fails on the buggy build (the pill is present); the recording
        # captured that failure regardless.
        expect(page.locator(_ERROR_PILL)).to_have_count(0, timeout=1_000)
    finally:
        if viewer is not None:
            try:
                if viewer.stdin is not None:
                    viewer.stdin.close()
            except OSError:
                pass
            viewer.terminate()
            try:
                viewer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait(timeout=5)
        page.close()
        context.close()  # flush the .webm recording
