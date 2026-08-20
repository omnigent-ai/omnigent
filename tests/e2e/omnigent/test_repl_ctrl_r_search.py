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

from pathlib import Path
from typing import Any

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot
from tests.e2e.omnigent.conftest import configure_mock_llm

_MODEL = "mock-ctrl-r-model"
_HARNESS = "openai-agents"

# A prompt with a clearly-unique substring we can search for.
# The substring is chosen so it can't appear organically in
# startup banners or prompt-toolkit chrome.
_NEEDLE = "zxqw-unique-history-token"
_PROMPT = f"please just say ok ({_NEEDLE})"

# prompt-toolkit's default reverse-search toolbar prompt. This
# is the literal text the SearchToolbar widget paints when
# ``start_reverse_incremental_search`` runs; asserting on it
# proves we actually entered search mode.
_SEARCH_PROMPT_MARKER = "I-search backward"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0

# How long to wait for the Ctrl+R + substring render cycle. The
# keystrokes are local; the delay is purely for prompt-toolkit's
# min_redraw_interval + render tick.
_SEARCH_DRAIN_TIMEOUT = 3.0
_ACCEPT_DRAIN_TIMEOUT = 3.0


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
            {"text": "ok"},
            {"text": "ok again"},
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
    try:
        # Match the visible prompt marker rather than the bottom-
        # toolbar state text: under pexpect the prompt-toolkit CPR
        # handshake can suppress ``state: sleeping`` even when the
        # REPL is ready.
        child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=r"working",
            completion_pattern=r"❯ ",
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
        # Collect the post-Ctrl+R + post-substring render
        # frames. drain_for handles the case where prompt-
        # toolkit splits the search-toolbar paint and the
        # match-surfacing paint into separate frames.
        search_drain = drain_for(child, _SEARCH_DRAIN_TIMEOUT)

        # Enter should accept the current reverse-search match, leave
        # search mode, and keep the matched command in the normal input
        # buffer for editing/submission. This regressed when the custom
        # REPL key map started Ctrl+R search but did not explicitly wire
        # search-mode Enter to prompt-toolkit's accept_search handler.
        child.send("\r")
        accept_drain = drain_for(child, _ACCEPT_DRAIN_TIMEOUT)
        # Press Ctrl+G to cancel any still-active search mode, then submit.
        # If Enter worked, Ctrl+G is a no-op against the normal input buffer
        # and the recalled prompt is submitted. If Enter did not work, Ctrl+G
        # exits search mode without accepting the match, leaving no prompt to
        # submit; the wait below times out before observing a turn.
        child.sendcontrol("g")
        drain_for(child, _ACCEPT_DRAIN_TIMEOUT)
        child.send("\r")
        submit_drain = drain_for(child, _ACCEPT_DRAIN_TIMEOUT)
        try:
            await_turn_complete(
                child,
                running_timeout=_RUNNING_TIMEOUT,
                completion_timeout=_COMPLETION_TIMEOUT,
                running_marker=r"working",
                completion_pattern=r"❯ ",
            )
            accepted_search_submits = True
        except Exception:
            accepted_search_submits = False
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
    finally:
        if not child.closed:
            child.close(force=True)

    search_stripped = strip_ansi(search_drain)
    accept_stripped = strip_ansi(accept_drain)
    tail_stripped = strip_ansi(child.before or "")
    submit_stripped = strip_ansi(submit_drain)
    combined_stripped = (
        search_stripped + "\n" + accept_stripped + "\n" + submit_stripped + "\n" + tail_stripped
    )

    observed: dict[str, Any] = {
        "exit_code": exit_code,
        # Proof that Ctrl+R put the REPL into search mode — the
        # SearchToolbar paints "I-search backward: " only while
        # an incremental search is active.
        "search_toolbar_visible": _SEARCH_PROMPT_MARKER in combined_stripped,
        # The matched history entry should surface in the input
        # area. Searching for the unique token confirms the
        # entry was found — not just that search was started.
        "needle_surfaced": _NEEDLE in search_stripped,
        # After pressing Enter, the SearchToolbar should disappear,
        # and the matched entry should still be present in the normal
        # input area. This is the actual "select result" behavior
        # users expect from Ctrl+R.
        "enter_accepts_search": accepted_search_submits,
    }
    diffs = compare_snapshot("test_repl_ctrl_r_search", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+R reverse-search:\n"
        + "\n".join(diffs)
        + f"\n\nsearch-drain stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
