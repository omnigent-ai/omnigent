"""E2E: the hidden terminal's scrollbar must not paint over the chat bar on iOS.

Reported journey (iOS app, iPhone): open a terminal-first session, send a
message so the agent starts working in its terminal, and a translucent strip —
the hidden terminal surface's xterm scrollbar — paints over the chat
transcript and the chat bar, overlapping the "Send a follow-up" composer text.
Because the strip is also hit-testable (xterm gives it ``z-index: 11``), taps
aimed at the chat bar can land on the ghost scrollbar instead.

Mechanism: the pre-warmed terminal overlay in ``ChatPage.tsx`` is hidden with
Tailwind's ``invisible`` utility (inherited ``visibility: hidden``), but xterm
marks its active scrollbar with a ``visible`` class — and Tailwind's
``.visible { visibility: visible }`` utility overrides the inherited hide, so
the scrollbar re-reveals itself inside the "hidden" overlay and paints over
chat.

The test drives the user journey at an iPhone-13 viewport under a stubbed iOS
shell bridge and asserts the user-facing invariant: while the chat view is
frontmost, no element of the hidden terminal surface is effectively painted
(visible, not opacity-hidden, non-empty box) or able to swallow taps. It
FAILS on the unfixed build (the ghost scrollbar paints while the agent's
terminal streams output) and passes once the overlay is hidden with a
mechanism descendants can't undo.

Two preconditions are asserted loudly so the test can never pass vacuously
when the fixture journey silently breaks (e.g. ambient ``OMNIGENT_*`` config
re-routes the scripted agent away from the mock LLM and the turn errors):
the scripted turn must complete, and the hidden xterm's scrollbar must show
scroll activity (a JS-driven class toggle no CSS hiding fix suppresses).
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import tarfile
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)

# iPhone-13 form factor: matches the report (iOS app) and keeps the SPA in
# the mobile layout where the chat bar spans the full width.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# Minimal stand-in for the iOS WKWebView bridge (same feature-detection
# stubbing as test_ios_switcher_in_header.py) so ``isIOSShell()`` sees the
# iOS shell and the SPA renders its iOS-app chrome.
_IOS_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "ios",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onOpenPath: function () { return function () {}; },
  onSidebarDrag: function () { return function () {}; },
  setServerSwitcherHidden: function () {},
  setViewMode: function () {},
  onViewModeChanged: function () { return function () {}; },
  onNativeInsets: function (callback) {
    callback({ topBar: 36, bottomBar: 48 });
    return function () {};
  },
};
"""

_TUI_AGENT_NAME = "tui_ghost_demo"

# The scripted turn's final assistant sentence; its appearance in the
# transcript proves the mock-LLM turn actually ran (precondition A).
_TURN_DONE_TEXT = "The tui terminal is running"

# ~40 seconds of continuous terminal output: enough scrollback to give xterm
# a scrollbar, and enough ongoing scrolling that the scrollbar keeps
# revealing itself for the whole sampling window below.
_STREAM_COMMAND = (
    'i=0; while [ "$i" -lt 1800 ]; do i=$((i+1)); echo "tui output line $i"; sleep 0.02; done'
)

# Terminal named ``tui`` so the launched PTY's resource id is
# ``terminal_tui_main`` — an AGENT_TERMINAL_IDS member, i.e. the same
# agent-pane shape an ``omnigent claude``-style terminal-first session has,
# which the warm ChatPage overlay auto-selects and attaches to.
_TUI_AGENT_YAML = f"""\
name: {_TUI_AGENT_NAME}
prompt: |
  You are a deterministic terminal test assistant. When the user asks you
  to start the tui, you MUST do exactly this sequence:

  1. Call sys_terminal_launch with terminal="tui" and session="main".
  2. Call sys_terminal_send with terminal="tui", session="main", and the
     long-output shell command you were configured with.
  3. Reply with exactly one short sentence confirming the tui terminal is
     running.

  Do not ask for confirmation; do not call any other tools.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

terminals:
  tui:
    command: bash
    args: ["--noprofile", "--norc"]
    os_env:
      type: caller_process
      cwd: .
      sandbox:
        type: none
"""

# One consistent in-page sample: every scrollbar node living inside a hidden
# (aria-hidden) terminal overlay that is *effectively painted* — its own
# computed visibility says visible and no ancestor hides it via opacity or
# display. For each hit it also records whether the strip overlaps the chat
# bar (the composer) and whether it swallows the hit-test at its center.
# ``active`` reports xterm's own scrollbar state classes (``visible`` /
# ``fade``): xterm toggles them from JS while the terminal scrolls, so they
# flip on fixed and unfixed builds alike — a paint-independent signal that
# the stream really is scrolling the hidden terminal (precondition B).
_SAMPLE_JS = """
() => {
  const wrappers = [...document.querySelectorAll('div[aria-hidden="true"]')].filter(
    (w) => w.querySelector('.xterm'),
  );
  const composer = document.querySelector('textarea[aria-label="Message the agent"]');
  const bar = composer ? (composer.closest('form') || composer) : null;
  const barRect = bar ? bar.getBoundingClientRect() : null;
  const hits = [];
  let scrollbars = 0;
  let active = false;
  for (const w of wrappers) {
    for (const sb of w.querySelectorAll('.xterm-scrollable-element > .scrollbar')) {
      scrollbars += 1;
      if (/(^| )(visible|fade)( |$)/.test(sb.className)) active = true;
      const cs = getComputedStyle(sb);
      let ancestorHidden = false;
      for (let n = sb; n; n = n.parentElement) {
        const acs = getComputedStyle(n);
        if (acs.display === 'none' || parseFloat(acs.opacity) === 0) {
          ancestorHidden = true;
          break;
        }
      }
      const r = sb.getBoundingClientRect();
      const painted =
        cs.visibility === 'visible' && !ancestorHidden && r.width > 0 && r.height > 0;
      if (!painted) continue;
      const hit = document.elementFromPoint(
        Math.min(r.left + r.width / 2, innerWidth - 1),
        Math.min(r.top + r.height / 2, innerHeight - 1),
      );
      hits.push({
        classes: sb.className,
        zIndex: cs.zIndex,
        rect: { x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height) },
        overlapsChatBar: !!barRect &&
          r.left < barRect.right && r.right > barRect.left &&
          r.top < barRect.bottom && r.bottom > barRect.top,
        interceptsTaps: !!hit && w.contains(hit),
        chatBar: barRect ? { x: Math.round(barRect.x), y: Math.round(barRect.y),
                             w: Math.round(barRect.width), h: Math.round(barRect.height) } : null,
      });
    }
  }
  return { hits, wrappers: wrappers.length, scrollbars, active };
}
"""


def _mark_terminal_first(base_url: str, session_id: str) -> None:
    """Stamp the session terminal-first (``omnigent.ui = terminal``).

    The warm terminal overlay only mounts for terminal-first sessions, so the
    label is the journey's precondition — the same one ``omnigent claude`` /
    ``omnigent codex`` sessions carry.

    :param base_url: Spawned server base URL.
    :param session_id: Session to label.
    """
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    )
    response.raise_for_status()


def _ensure_chat_frontmost(page: Page) -> None:
    """Flip back to the Chat view if the terminal took the screen over.

    A terminal-first session may auto-open its terminal pane when the agent
    launches it mid-turn; the reported journey has the user looking at chat.

    :param page: Playwright page on the session route.
    """
    terminal_view = page.locator('[data-testid="main-terminal-view"]')
    if terminal_view.count() == 0:
        return
    if terminal_view.first.get_attribute("data-visible") == "true":
        chat_button = page.get_by_role("button", name="Chat view")
        if chat_button.count():
            chat_button.first.click()


def _wait_for_turn_done(page: Page, timeout_s: float = 120.0) -> None:
    """Wait until the scripted turn's final assistant reply is in the DOM.

    Keeps the chat view frontmost while polling (the terminal pane may
    auto-open mid-turn and hide the transcript). Failing here means the
    fixture journey never ran — e.g. the agent's LLM call was routed away
    from the mock server by ambient environment config — so the test stops
    loudly instead of passing without exercising the bug.

    :param page: Playwright page on the session route.
    :param timeout_s: How long to wait for the reply.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _ensure_chat_frontmost(page)
        if page.get_by_text(_TURN_DONE_TEXT, exact=False).count() > 0:
            return
        page.wait_for_timeout(500)
    pytest.fail(
        "Precondition failed: the scripted agent turn never completed (no "
        f"assistant reply containing {_TURN_DONE_TEXT!r} within {timeout_s:.0f}s). "
        "The journey was not exercised — check the spawned server/runner can "
        "reach the mock LLM (ambient OMNIGENT_*/gateway config env can "
        "re-route the scripted agent's model and 404 the turn)."
    )


@pytest.fixture
def tui_ghost_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Runner-bound session whose agent launches a real ``tui:main`` PTY.

    The mock LLM is scripted (content-routed on "start the tui") to launch
    the agent terminal and stream ~40s of output into it — the "agent starts
    working" half of the reported journey, against a real tmux-backed PTY.

    :param live_server: Live server base URL (fixture).
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_launch_tui",
                        "name": "sys_terminal_launch",
                        "arguments": json.dumps({"terminal": "tui", "session": "main"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_stream_tui",
                        "name": "sys_terminal_send",
                        "arguments": json.dumps(
                            {"terminal": "tui", "session": "main", "text": _STREAM_COMMAND}
                        ),
                    }
                ]
            },
            {"text": "The tui terminal is running and streaming output."},
        ],
        key="tui-ghost-scrollbar",
        match="start the tui",
    )
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    # Inline omnigent-shorthand bundle with a non-config.yaml name so it
    # routes through the compat adapter, which parses `terminals:` (matches
    # the terminal_session fixture).
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = _TUI_AGENT_YAML.encode()
        info = tarfile.TarInfo(name=f"{_TUI_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=10.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            try:
                reset_mock_llm(mock_llm_server_url)
            finally:
                if respawned_runner is not None:
                    respawned_runner.terminate()
                    try:
                        respawned_runner.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        respawned_runner.kill()
                        respawned_runner.wait(timeout=5)


def test_hidden_terminal_scrollbar_stays_off_the_chat_bar(
    page: Page,
    tui_ghost_session: tuple[str, str],
) -> None:
    """While chat is frontmost, the hidden terminal must not paint over it.

    Journey: open a terminal-first session in the (stubbed) iOS shell at an
    iPhone-13 viewport → send a message so the agent starts working in its
    terminal → stay on the chat view while the terminal streams output → the
    chat bar and transcript must stay clean. On the unfixed build the hidden
    surface's xterm scrollbar escapes the ``invisible`` wrapper, paints a
    translucent strip over the chat (overlapping the composer's
    "Send a follow-up…" chat bar), and swallows taps aimed at it.

    :param page: Playwright page fixture (fresh context per test).
    :param tui_ghost_session: ``(base_url, session_id)`` of a runner-bound
        session whose agent launches + streams into a real ``tui:main`` PTY.
    """
    base_url, session_id = tui_ghost_session
    _mark_terminal_first(base_url, session_id)

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)

    # The user's action: ask the agent to start working. The scripted turn
    # launches the agent terminal and streams ~40s of output into it.
    composer.fill("start the tui terminal")
    page.get_by_role("button", name="Send", exact=True).click()

    # Precondition A: the scripted turn actually ran (assistant reply landed).
    _wait_for_turn_done(page)

    # Precondition: the warm (hidden) terminal overlay mounted and its xterm
    # attached to the real PTY behind the chat view. Without this the test
    # would pass vacuously on any build.
    page.wait_for_selector('div[aria-hidden="true"] .xterm', state="attached", timeout=90_000)
    _ensure_chat_frontmost(page)
    page.wait_for_selector(
        'div[aria-hidden="true"] .xterm-scrollable-element > .scrollbar',
        state="attached",
        timeout=90_000,
    )

    # Watch the chat view while the hidden terminal streams. Any sample in
    # which a scrollbar node from the hidden overlay is effectively painted
    # is the bug; collecting a few paints a clearer failure than one.
    deadline = time.monotonic() + 30.0
    painted: list[dict] = []
    samples = 0
    scroll_activity = False
    while time.monotonic() < deadline:
        _ensure_chat_frontmost(page)
        sample = page.evaluate(_SAMPLE_JS)
        samples += 1
        scroll_activity = scroll_activity or sample["active"]
        if sample["hits"]:
            painted.extend(sample["hits"])
            if len(painted) >= 5:
                break
        page.wait_for_timeout(150)

    print(
        f"[ghost-scrollbar] samples={samples} painted={len(painted)} "
        f"wrappers={sample['wrappers']} scrollbars={sample['scrollbars']} "
        f"scroll_activity={scroll_activity}"
    )
    for hit in painted[:5]:
        print(f"[ghost-scrollbar] painted: {hit}")

    # Precondition B: the hidden terminal really streamed and scrolled (xterm
    # flipped its scrollbar state classes). Without this a broken fixture
    # journey would sample a dormant terminal and pass on any build.
    if not scroll_activity:
        pytest.fail(
            "Precondition failed: the hidden terminal's xterm scrollbar never "
            f"showed scroll activity in {samples} samples — the PTY stream "
            "did not reach the warm overlay, so the journey was not "
            "exercised. Check the scripted terminal launch/stream (runner "
            "online, tmux available, mock LLM reachable)."
        )

    assert not painted, (
        "Hidden terminal surface painted over the chat view: its xterm "
        f"scrollbar escaped the hidden wrapper in {len(painted)} sample(s) "
        f"out of {samples} (first: {painted[0]}). overlapsChatBar=True means "
        "the strip covered the composer's chat bar (the reported "
        '"Send a follow-up" overlap); interceptsTaps=True means it would '
        "swallow taps aimed at the chat surface."
    )
