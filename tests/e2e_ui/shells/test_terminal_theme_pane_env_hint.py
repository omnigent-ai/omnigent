"""E2E: the resolved terminal theme must reach the pane's PTY as a background hint.

``Settings → Appearance → Terminal theme`` (Match app / Light / Dark) resolves
to a light or dark xterm.js ``ITheme`` — it repaints the *canvas* the pane's
bytes are drawn on (``data-terminal-theme`` is the DOM signal, see
``sessions/test_terminal_theme.py``). The process *inside* the pane picks its
own ANSI colors, and it can only pick readable ones if something tells it what
background it is rendering against. The conventional carrier for that hint is
the ``COLORFGBG`` environment variable (``<fg>;<bg>``, background field last:
0–6/8 = dark, 7/15 = light — the heuristic vim/neovim use for ``bg=``).

Today nothing propagates the resolved theme into the PTY environment: the
attach path pins ``TERM`` and normalizes locale/secrets only. So a dark-mode
app with the default "Match app" terminal renders a dark canvas while the
harness TUI, told nothing, may assume a light background and emit dark-on-dark,
unreadable output; flipping the Terminal theme setting repaints the canvas but
still tells the process nothing.

Two contracts are pinned, one per launched shell:

1. **Match app (dark)** — with the app pinned Dark and the terminal theme on
   its default "Match app", a freshly launched shell's environment must carry
   a background hint that says *dark* (matching the pane's resolved
   ``data-terminal-theme="dark"``).
2. **Pinned Light under a dark app** — with the terminal theme pinned Light,
   a freshly launched shell must be told *light*, proving the setting reaches
   the process and not just the canvas.

The pane's stdout is painted to a WebGL canvas and is not DOM-readable, so the
probe records the environment to an absolute ``tmp_path`` file (the shell and
this test share a host — same pattern as ``shells/test_new_shell.py``'s wheel
test) and the test asserts on the file. The probe also ``tee``'s to the pane
so a human watching sees the value. Both tests fail while the hint is not
propagated (``COLORFGBG`` unset in the pane).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# COLORFGBG's last ";"-separated field is the background color. 0-6 and 8 are
# the dark palette entries, 7 and 15 the light ones (the vim/neovim bg= rule).
_DARK_BG_CODES = {"0", "1", "2", "3", "4", "5", "6", "8"}
_LIGHT_BG_CODES = {"7", "15"}


def _pick_appearance(page: Page, base_url: str, app_mode: str, terminal_mode: str) -> None:
    """Pin the app theme and the terminal theme via the Appearance radio cards."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Terminal theme")).to_be_visible(timeout=30_000)
    app_card = page.get_by_test_id(f"theme-{app_mode}")
    app_card.click()
    expect(app_card).to_have_attribute("aria-checked", "true")
    terminal_card = page.get_by_test_id(f"terminal-theme-{terminal_mode}")
    terminal_card.click()
    expect(terminal_card).to_have_attribute("aria-checked", "true")


def _open_new_shell(page: Page) -> None:
    """Create a shell via the workspace rail's "+" → Shell menu (user-driven)."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _connected_shell_terminal(page: Page) -> Locator:
    """Wait for the freshly launched shell's xterm to mount + connect in the rail."""
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("button", name=re.compile(r"^Close zsh · u-"))).to_be_visible(
        timeout=60_000
    )
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=20_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def _type_into_pane(page: Page, terminal_view: Locator, line: str) -> None:
    """Type one command line into the connected pane and press Enter."""
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(line)
    page.keyboard.press("Enter")


def _probe_pane_background_hint(
    page: Page, terminal_view: Locator, probe_file: Path
) -> tuple[str, str]:
    """Record the pane shell's COLORFGBG/TERM to ``probe_file`` and return them.

    Types a probe that ``tee``'s the values to the pane (visible to a human)
    and to an absolute host path (readable by the test — xterm paints to a
    WebGL canvas, so pane stdout is not in the DOM).

    :returns: ``(colorfgbg, term)`` — ``colorfgbg`` is the literal value or
        ``"UNSET"`` when the variable is absent from the pane environment.
    """
    _type_into_pane(
        page,
        terminal_view,
        f"echo \"COLORFGBG=${{COLORFGBG-UNSET}} TERM=${{TERM-UNSET}}\" | tee '{probe_file}'",
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if probe_file.exists() and probe_file.read_text().strip():
            break
        page.wait_for_timeout(500)
    assert probe_file.exists() and probe_file.read_text().strip(), (
        "the pane shell never wrote the environment probe file — the typed probe did not execute"
    )
    fields = dict(
        part.split("=", 1) for part in probe_file.read_text().strip().split() if "=" in part
    )
    assert "COLORFGBG" in fields, f"malformed probe output: {probe_file.read_text()!r}"
    return fields["COLORFGBG"], fields.get("TERM", "")


def test_match_app_dark_pane_env_carries_dark_background_hint(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """A dark "Match app" pane must tell its PTY it renders on a dark background.

    Pins the app Dark with the default "Match app" terminal theme, launches a
    shell, and confirms the pane resolved dark. It then shows what a
    light-background-assuming TUI emits on that pane (dark SGR text — the
    unreadable dark-on-dark render a human sees) and probes the shell's
    environment: the resolved dark theme must have been propagated as a
    dark-background ``COLORFGBG`` hint. While nothing is propagated the probe
    reads ``UNSET`` and this test fails — the harness process has no way to
    know the background it paints against.
    """
    base_url, session_id = terminal_session
    probe_file = tmp_path / "pane-env-dark.txt"

    _pick_appearance(page, base_url, app_mode="dark", terminal_mode="auto")

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_shell_terminal(page)
    expect(terminal_view).to_have_attribute("data-terminal-theme", "dark")
    # Let the pane shell finish printing its first prompt before typing.
    page.wait_for_timeout(1_500)

    # The user-visible failure: a TUI that assumed a light background paints
    # dark foreground colors, which vanish on the pane's dark canvas.
    _type_into_pane(
        page,
        terminal_view,
        "printf 'A light-background TUI paints dark text like this: "
        "\\e[30m[ can you read this dark-on-dark line? ]\\e[0m <- end of line\\n'",
    )
    page.wait_for_timeout(1_500)

    hint, term = _probe_pane_background_hint(page, terminal_view, probe_file)
    page.wait_for_timeout(1_500)

    assert hint != "UNSET", (
        "the web UI's resolved terminal theme (dark) never reached the pane's PTY: "
        f"COLORFGBG is unset in the shell environment (TERM={term!r}). A harness TUI "
        "cannot know it renders on the dark canvas, so one that assumes a light "
        "background emits unreadable dark-on-dark text."
    )
    bg = hint.rsplit(";", 1)[-1].strip()
    assert bg in _DARK_BG_CODES, (
        f"the pane resolved dark but COLORFGBG={hint!r} does not indicate a dark "
        f"background (background field {bg!r})"
    )


def test_pinned_light_pane_env_carries_light_background_hint(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Pinning the terminal theme must inform the PTY, not just repaint the canvas.

    Under a dark app, pins the terminal theme to Light and launches a shell:
    the pane resolves light (``data-terminal-theme="light"``), and the shell's
    environment must carry a light-background ``COLORFGBG`` hint. While the
    setting only restyles the xterm canvas, the probe reads ``UNSET`` and this
    test fails — flipping the setting cannot fix a harness that guessed the
    wrong background, because the process is never told anything.
    """
    base_url, session_id = terminal_session
    probe_file = tmp_path / "pane-env-light.txt"

    _pick_appearance(page, base_url, app_mode="dark", terminal_mode="light")

    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)
    terminal_view = _connected_shell_terminal(page)
    expect(terminal_view).to_have_attribute("data-terminal-theme", "light")
    # Let the pane shell finish printing its first prompt before typing.
    page.wait_for_timeout(1_500)

    hint, term = _probe_pane_background_hint(page, terminal_view, probe_file)
    page.wait_for_timeout(1_500)

    assert hint != "UNSET", (
        "the pinned Light terminal theme repainted the canvas but never reached the "
        f"pane's PTY: COLORFGBG is unset in the shell environment (TERM={term!r}), so "
        "the setting cannot inform the harness process which background it is on."
    )
    bg = hint.rsplit(";", 1)[-1].strip()
    assert bg in _LIGHT_BG_CODES, (
        f"the pane resolved light but COLORFGBG={hint!r} does not indicate a light "
        f"background (background field {bg!r})"
    )
