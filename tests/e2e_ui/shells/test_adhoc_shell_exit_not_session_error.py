"""E2E: a clean ad-hoc shell exit must not be logged as a session ERROR.

A user opens an ad-hoc shell from the Workspace rail's "+" menu and ends it
the normal way — typing ``exit`` at the prompt. The shell process
terminates, tmux tears its session down, and the runner's terminal idle
watcher (``_idle_watch_loop_threaded`` in ``omnigent/inner/terminal.py``)
notices tmux is gone. Reporting that expected lifecycle event at ERROR
level::

    tmux unavailable after 3 consecutive probes for terminal zsh:u-abc123

is what the debug-logs KPI pipeline counts as a session error, so every
clean ad-hoc shell exit would show up as an Omnigent defect in the
error-rate dashboards even though nothing failed for the user
(auxiliary-terminal exits publish only ``session.resource.deleted`` — no
user-facing error at all).

The regression contract asserted here: a clean, user-initiated ad-hoc shell
exit produces NO ERROR-level runner log records for that terminal. The exit
may (and should) still be observed and logged — just not at a level that the
telemetry pipeline attributes as a session error.

Uses the ``terminal_session`` fixture (agent declaring a ``zsh`` terminal
that actually runs ``bash --noprofile --norc``); shell creation via the "+"
menu needs no LLM turn, so no chat message is ever sent.
"""

from __future__ import annotations

import contextlib
import re
import time

import httpx
from playwright.sync_api import Page, expect

from omnigent.process_logging import process_log_dir
from tests.e2e_ui.conftest import open_right_rail


def _list_user_shells(base_url: str, session_id: str) -> list[dict]:
    """Return the session's user-created (``u-…`` keyed) shell terminals.

    :param base_url: Live server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound session id.
    :returns: Terminal inventory rows whose ``metadata.session_key`` carries
        the ``u-`` prefix the web UI mints for "+" → Shell creations.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        params={"order": "asc", "limit": 1000},
        timeout=10.0,
    )
    resp.raise_for_status()
    return [
        row
        for row in resp.json()["data"]
        if str((row.get("metadata") or {}).get("session_key", "")).startswith("u-")
    ]


def _runner_error_lines_for(terminal_ref: str) -> list[str]:
    """Collect ERROR-level runner log lines that mention *terminal_ref*.

    Scans every runner process log (``<data-dir>/logs/runner/*.log``; the
    test process and the spawned runner share the same environment, so
    ``process_log_dir`` resolves identically in both). The ``u-…`` session
    key is per-creation unique, so the filter cannot match another test's
    terminal even when shards share the log directory.

    :param terminal_ref: ``"<terminal_name>:<session_key>"``, e.g.
        ``"zsh:u-abc123"`` — the exact rendering the watcher logs.
    :returns: Matching ``ERROR``-level lines, prefixed with the log file name.
    """
    matches: list[str] = []
    log_dir = process_log_dir("runner")
    if not log_dir.is_dir():
        return matches
    for log_file in sorted(log_dir.glob("*.log")):
        try:
            text = log_file.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if terminal_ref in line and line.startswith("ERROR"):
                matches.append(f"{log_file.name}: {line}")
    return matches


def test_adhoc_shell_exit_is_not_logged_as_session_error(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Typing ``exit`` in a "+"-menu shell must not emit an ERROR log record.

    Drives the full user journey: create the shell from the rail's "+" menu,
    wait for its xterm to connect, type ``exit``, and wait for the runner to
    observe the exit (the terminal resource is evicted from the inventory).
    The watcher writes its log record strictly before the exit callback that
    evicts the resource, so once the terminal is gone the log is settled.
    """
    base_url, session_id = terminal_session

    page.goto(f"{base_url}/c/{session_id}")

    # Create the ad-hoc shell exactly as a user does: "+" → Shell. A single
    # declared terminal type launches directly (POST /resources/terminals
    # with a freshly minted ``u-…`` session key).
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()

    # Wait for the shell's xterm to connect before typing — keystrokes sent
    # before the WS attach opens are dropped.
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

    shells = _list_user_shells(base_url, session_id)
    assert len(shells) == 1, f"expected exactly one user shell, got: {shells}"
    metadata = shells[0].get("metadata") or {}
    terminal_ref = f"{metadata['terminal_name']}:{metadata['session_key']}"

    def _type_exit() -> None:
        # xterm renders to a canvas; its hidden helper textarea takes input.
        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type("exit")
        page.keyboard.press("Enter")

    _type_exit()

    # The runner's idle watcher probes once per second and confirms the exit
    # after 3 consecutive failed probes, then evicts the terminal resource.
    # Poll the inventory until the shell is gone; retype ``exit`` at a slow
    # cadence in case the first keystrokes raced the shell's startup.
    deadline = time.time() + 90
    last_retype = time.time()
    while time.time() < deadline:
        if not _list_user_shells(base_url, session_id):
            break
        if time.time() - last_retype > 15:
            last_retype = time.time()
            # The view may unmount between the inventory poll and the
            # retype once the exit lands; the next poll observes it.
            with contextlib.suppress(Exception):
                _type_exit()
        time.sleep(1.0)
    else:
        raise AssertionError(
            f"terminal {terminal_ref} was never evicted after typing 'exit'; "
            "the runner did not observe the shell exit"
        )

    # A clean user-driven shell exit is expected lifecycle, not a session
    # error: no runner log record about this terminal may be ERROR-level.
    # A regression logs "tmux unavailable after 3 consecutive probes for
    # terminal <ref>" at ERROR, which the debug-logs KPI counts as a
    # session error.
    error_lines = _runner_error_lines_for(terminal_ref)
    assert not error_lines, (
        "clean ad-hoc shell exit was logged at ERROR level (counted as a "
        "session error by the debug-logs KPI):\n" + "\n".join(error_lines)
    )
