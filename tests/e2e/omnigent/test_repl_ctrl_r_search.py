"""Phase 0 characterization test — Ctrl+R reverse-incremental search.

Migrated to mock LLM: uses canned responses for the LLM turns so the
test is deterministic and requires no real Databricks credentials.

Submits a prompt carrying a unique substring, presses ``Ctrl+R``,
types the substring, and asserts (a) the reverse-search prompt
activates, (b) the history entry containing the substring surfaces in
the input area, and (c) pressing Enter accepts the match back into
the input buffer.

**What breaks if this fails:**
- ``omnigent.cli`` removes the ``@kb.add("c-r")`` binding that
  delegates to prompt-toolkit's
  ``start_reverse_incremental_search``.
- ``omnigent.cli`` forgets to bind Enter while searching to
  prompt-toolkit's ``accept_search``, so the surfaced match
  cannot be selected.
- ``SearchToolbar`` stops rendering its default
  ``"I-search backward: "`` prompt — breaks the "search mode
  activated" observation.
- The input-area buffer's history search loses the submitted
  prompts, so a substring match has nothing to surface.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
REPL pexpect suite — "Ctrl+R reverse-search".
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests._helpers.terminal_screen import TerminalScreen
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-ctrl-r-model"
_HARNESS = "openai-agents"

# A prompt with a clearly-unique substring we can search for.
# The substring is chosen so it can't appear organically in
# startup banners or prompt-toolkit chrome.
_NEEDLE = "zxqw-unique-history-token"
_PROMPT = f"please just say ok ({_NEEDLE})"
_INITIAL_RESPONSE = "initial-ctrl-r-turn-finished"
_RECALLED_RESPONSE = "recalled-ctrl-r-turn-finished"

# prompt-toolkit's default reverse-search toolbar prompt. This
# is the literal text the SearchToolbar widget paints when
# ``start_reverse_incremental_search`` runs; asserting on it
# proves we actually entered search mode.
_SEARCH_PROMPT_MARKER = "I-search backward"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0
# Keystrokes are local; this bounds a render on a loaded CI runner.
_SEARCH_TIMEOUT = 15.0


def _turn_finished(response: str) -> Callable[[str], bool]:
    """Match a committed response once the REPL is idle again."""
    return lambda screen: f"◆ {response}" in screen and "state: sleeping" in screen


def _input_line(screen: str) -> str | None:
    """Return the prompt line nearest above the bottom toolbar, i.e. the input area."""
    lines = screen.splitlines()
    toolbar = max((index for index, line in enumerate(lines) if "state:" in line), default=None)
    if toolbar is None:
        return None
    for line in reversed(lines[:toolbar]):
        if line.strip().startswith("❯"):
            return line.strip()
    return None


def _search_state(screen: str) -> tuple[bool, bool]:
    """Return whether search is active and the input area shows the match."""
    searching = any(
        line.startswith(_SEARCH_PROMPT_MARKER) and line.endswith(_NEEDLE)
        for line in screen.splitlines()
    )
    return searching, searching and _input_line(screen) == f"❯ {_PROMPT}"


def _search_accepted(screen: str) -> bool:
    """Search has ended and the match stays in the input area for editing."""
    return _SEARCH_PROMPT_MARKER not in screen and _input_line(screen) == f"❯ {_PROMPT}"


def test_repl_ctrl_r_reverse_search(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Submit one prompt, press Ctrl+R, type a substring, and
    verify the search toolbar appears and the matching history
    entry is surfaced.

    Uses the mock LLM server for deterministic responses.

    :param omnigent_python: Interpreter with omnigent +
        openai-agents installed.
    :param omnigent_repo_root: Working directory for the
        subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    # Two turns: the initial prompt and the re-submitted prompt via Ctrl+R.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": _INITIAL_RESPONSE},
            {"text": _RECALLED_RESPONSE},
        ],
        key=_MODEL,
    )
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    # prompt-toolkit repaints only changed cells, so match the rendered screen.
    screen = TerminalScreen(child)
    try:
        screen.wait_for(
            lambda text: "❯" in text and "state: sleeping" in text,
            timeout=_BOOT_TIMEOUT,
            what="the idle prompt",
        )
        submit_prompt(child, _PROMPT)
        # Search only once the first turn has committed; pressing Ctrl+R while
        # it finishes races the completion repaint.
        screen.wait_for(
            _turn_finished(_INITIAL_RESPONSE),
            timeout=_COMPLETION_TIMEOUT,
            what="the first turn to finish",
        )
        # Enter reverse-search mode. prompt-toolkit swaps the
        # input area focus to the search toolbar and redraws
        # with the "I-search backward: " prompt.
        child.sendcontrol("r")
        # Type the unique substring. Each character narrows the
        # search; prompt-toolkit's default match behavior is to
        # surface the most recent history entry containing the
        # typed text.
        child.send(_NEEDLE)
        try:
            search_screen = screen.wait_for(
                lambda text: all(_search_state(text)),
                timeout=_SEARCH_TIMEOUT,
                what="the surfaced history match",
            )
        except TimeoutError:
            search_screen = screen.text()

        # Enter should accept the current reverse-search match, leave
        # search mode, and keep the matched command in the normal input
        # buffer for editing/submission. This regressed when the custom
        # REPL key map started Ctrl+R search but did not explicitly wire
        # search-mode Enter to prompt-toolkit's accept_search handler.
        child.send("\r")
        try:
            screen.wait_for(
                _search_accepted, timeout=_SEARCH_TIMEOUT, what="Enter to accept the match"
            )
            enter_accepts_search = True
        except TimeoutError:
            enter_accepts_search = False
        # Submitting the accepted entry proves the recalled prompt is usable.
        child.send("\r")
        with contextlib.suppress(TimeoutError):
            screen.wait_for(
                _turn_finished(_RECALLED_RESPONSE),
                timeout=_COMPLETION_TIMEOUT,
                what="the recalled prompt's turn to finish",
            )
        final_screen = screen.text()
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    search_toolbar_visible, needle_surfaced = _search_state(search_screen)
    observed: dict[str, Any] = {
        "exit_code": exit_code,
        # Proof that Ctrl+R put the REPL into search mode — the
        # SearchToolbar paints "I-search backward: " only while
        # an incremental search is active.
        "search_toolbar_visible": search_toolbar_visible,
        # The matched history entry should surface in the input
        # area while searching, confirming the
        # entry was found — not just that search was started.
        "needle_surfaced": needle_surfaced,
        # After pressing Enter, the SearchToolbar should disappear,
        # and the matched entry should still be present in the normal
        # input area. This is the actual "select result" behavior
        # users expect from Ctrl+R.
        "enter_accepts_search": enter_accepts_search,
    }
    diffs = compare_snapshot("test_repl_ctrl_r_search", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+R reverse-search:\n"
        + "\n".join(diffs)
        + f"\n\nscreen during search:\n{search_screen}"
        + f"\n\nfinal screen:\n{final_screen}"
    )
    requests = get_mock_requests(mock_llm_server_url, key=_MODEL)
    assert len(requests) == 2
    user_prompts = [
        item["content"] for item in requests[-1]["input"] if item.get("role") == "user"
    ]
    assert user_prompts == [_PROMPT, _PROMPT]
