"""E2E: the pane's resolved light/dark theme must reach the PTY it hosts.

The terminal pane resolves its background from Settings → Appearance
(app Mode + Terminal theme, ``resolveTerminalIsDark``) and paints xterm.js
accordingly — but that only styles the browser canvas. The process inside the
pane picks its own ANSI colors against the background it *believes* it is on,
so unless the resolved light/dark preference is propagated into the PTY's
environment (``COLORFGBG`` is the conventional hint), a TUI that assumes a
light background renders dark-on-dark under a dark pane and becomes
unreadable, and the "Match app" default produces that broken combination.

Two journeys, one per reported facet:

1. **Dark app + "Match app" terminal theme** → launch a shell from the
   workspace rail: the pane renders dark, so the freshly spawned shell must
   see a dark-background hint in its environment.
2. **Terminal theme pinned Light under a dark app** → launch a new shell: the
   pane renders light, so the shell must see a light-background hint — the
   setting must inform the process, not just repaint the canvas.

xterm paints to a WebGL canvas (stdout is not in the DOM), so the probe makes
the shell itself report the hint: it ``tee``s ``COLORFGBG`` (or ``UNSET``)
into a pytest tmp file the test reads back — the same runner-local file
side-channel ``test_terminal_cmd_line_editing.py`` relies on. Both tests use
the function-scoped ``terminal_session`` fixture and launch the shell via the
rail's "+" → Shell menu, mirroring ``shells/test_new_shell.py``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

HINT_VAR = "COLORFGBG"
# Conventional COLORFGBG background codes: 0/8 signal a dark background,
# 7/15 a light one (fg;bg — the last ;-separated field is the background).
DARK_BG_CODES = {"0", "8"}
LIGHT_BG_CODES = {"7", "15"}


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings → Appearance and wait for the theme controls."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Terminal theme")).to_be_visible(timeout=30_000)


def _pick_app_theme(page: Page, mode: str) -> None:
    """Pin the app theme via its Appearance radio card."""
    card = page.get_by_test_id(f"theme-{mode}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _pick_terminal_theme(page: Page, mode: str) -> None:
    """Pick a terminal-theme mode ("auto" = Match app) via its radio card."""
    card = page.get_by_test_id(f"terminal-theme-{mode}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _open_new_shell(page: Page) -> None:
    """Create a shell via the workspace rail's "+" → Shell menu."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _connected_shell(page: Page) -> tuple[Locator, Locator]:
    """Open a shell and return its connected terminal view + input textarea.

    Retries a shell stuck in ``connecting`` (the attach WS can lose the race
    with the PTY spawn on a loaded box), mirroring
    ``test_terminal_cmd_line_editing.py``.
    """
    rail = page.get_by_role("complementary", name="Workspace")
    last_error: AssertionError | None = None
    for _ in range(3):
        _open_new_shell(page)
        terminal_view = rail.get_by_test_id("terminal-view").last
        expect(terminal_view).to_be_visible(timeout=60_000)
        try:
            expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
        except AssertionError as exc:
            last_error = exc
            rail.get_by_role("button", name=re.compile(r"^Close ")).last.click()
            page.get_by_role("button", name=re.compile("Close")).last.click()
            page.wait_for_timeout(1_000)
            continue
        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        return terminal_view, textarea
    raise AssertionError(f"shell never connected after 3 attempts: {last_error}")


def _wait_for_file(path: Path, timeout_s: float) -> bool:
    """Poll for *path* to exist within the deadline."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.25)
    return False


def _await_shell_ready(page: Page, textarea: Locator, tmp_path: Path) -> None:
    """Prove the PTY's shell is accepting and executing typed input.

    The xterm attach connects before the shell finishes starting, so early
    keystrokes can be swallowed; a ``touch`` handshake (retyped a few times)
    makes later assertions fail only on the probed behavior.
    """
    ready = tmp_path / "shell_ready.txt"
    for _ in range(4):
        textarea.focus()
        page.keyboard.press("Control+c")
        page.keyboard.type(f"touch {ready}")
        page.keyboard.press("Enter")
        if _wait_for_file(ready, timeout_s=8.0):
            return
    raise AssertionError("shell never executed the readiness command; cannot probe the PTY env")


def _probe_hint(page: Page, textarea: Locator, tmp_path: Path, name: str) -> str:
    """Have the shell report its background hint, visibly and to a file.

    Types ``echo "COLORFGBG=${COLORFGBG-UNSET}" | tee <file>`` so the value
    is painted in the pane (the user-visible signal) *and* written where the
    test can read it back.
    """
    out = tmp_path / name
    textarea.focus()
    page.keyboard.type(f'echo "{HINT_VAR}=${{{HINT_VAR}-UNSET}}" | tee {out}')
    page.keyboard.press("Enter")
    assert _wait_for_file(out, timeout_s=15.0), "shell never wrote the hint probe file"
    page.wait_for_timeout(250)
    return out.read_text().strip()


def _assert_background_hint(printed: str, *, resolved: str) -> None:
    """Assert the probe output declares a background matching the pane."""
    value = printed.removeprefix(f"{HINT_VAR}=")
    assert value != "UNSET", (
        f"the {resolved}-rendered terminal pane launched a shell with no "
        f"background hint: {HINT_VAR} is unset in the PTY environment, so the "
        f"web UI theme never reaches the process — a TUI assuming the wrong "
        f"background renders unreadable (dark-on-dark under a dark pane)"
    )
    bg = value.rsplit(";", 1)[-1]
    expected = DARK_BG_CODES if resolved == "dark" else LIGHT_BG_CODES
    assert bg in expected, (
        f"{HINT_VAR}={value!r} does not declare a {resolved} background "
        f"(background code {bg!r}, expected one of {sorted(expected)})"
    )


def test_dark_app_match_terminal_shell_gets_dark_background_hint(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Dark app + "Match app" terminal theme: the PTY must learn it is on dark.

    Journey: pin the app to Dark with Terminal theme on Match app → open the
    session → launch a shell from the rail → the pane renders dark → the
    shell inside must carry a dark-background hint. Without it, the printed
    light-assuming demo line renders near-invisible on the dark pane and the
    probe reports UNSET.
    """
    base_url, session_id = terminal_session

    _open_appearance(page, base_url)
    _pick_app_theme(page, "dark")
    _pick_terminal_theme(page, "auto")

    page.goto(f"{base_url}/c/{session_id}")
    terminal_view, textarea = _connected_shell(page)
    expect(terminal_view).to_have_attribute("data-terminal-theme", "dark")
    _await_shell_ready(page, textarea, tmp_path)

    # What a light-background-assuming TUI does: near-black text, unreadable
    # on the dark pane the user is actually looking at.
    textarea.focus()
    page.keyboard.type(
        "printf '\\033[38;5;235mdemo: a light-assuming TUI prints near-black text\\033[0m\\n'"
    )
    page.keyboard.press("Enter")

    printed = _probe_hint(page, textarea, tmp_path, "hint_dark.txt")
    _assert_background_hint(printed, resolved="dark")


def test_light_pinned_terminal_informs_new_shell(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Pinning Terminal theme → Light must inform the process, not just repaint.

    Journey: under a Dark app, pin Terminal theme to Light → open the session
    → launch a new shell → the pane renders light → the freshly spawned shell
    must carry a light-background hint. On the buggy build the setting only
    restyles the canvas and the probe reports UNSET.
    """
    base_url, session_id = terminal_session

    _open_appearance(page, base_url)
    _pick_app_theme(page, "dark")
    _pick_terminal_theme(page, "light")

    page.goto(f"{base_url}/c/{session_id}")
    terminal_view, textarea = _connected_shell(page)
    expect(terminal_view).to_have_attribute("data-terminal-theme", "light")
    _await_shell_ready(page, textarea, tmp_path)

    printed = _probe_hint(page, textarea, tmp_path, "hint_light.txt")
    _assert_background_hint(printed, resolved="light")
