"""
REPL slash-command completion-reopen e2e test.

Spawns ``omnigent run tests/resources/agents/ask-demo`` under a
pseudo-TTY (pexpect), types a slash-command prefix, deletes part of
it with Backspace, and asserts the completion popup refreshes for
the remaining (still-matching) prefix.

User journey under test:

1. start the omnigent REPL
2. type ``/hel`` — the completion popup opens listing ``/help``
3. press Backspace twice, leaving ``/h``
4. expected: the popup reopens listing ``/help`` and ``/history``
   (both still match). Buggy behavior: prompt_toolkit clears the
   completion state on deletion and ``complete_while_typing`` only
   re-triggers on insertion, so the popup never reopens.

Assertion design — two constraints shape it:

- Output windows must contain ONLY what the REPL emitted during that
  window, so the popup rows rendered *while typing* can never satisfy
  the post-deletion assert. ``expect(TIMEOUT)``-style draining does
  not consume pexpect's buffer (each later call re-returns the same
  bytes), so the helpers here read the pty fd directly.
- prompt_toolkit's diff renderer repaints only changed cells: when a
  menu row mutates in place, unchanged leading cells (e.g. the ``" /"``
  of a command row) are skipped, so the exact string ``"/history"`` is
  not guaranteed on the wire even when the row is visibly present.
  The marker is the bare ``history`` — emitted by the ``/history`` row
  (or its description meta) whenever the menu (re)renders for the
  ``/h`` prefix, and by nothing else in this journey: the typed text
  is only ``/hel`` → ``/h``, and the toolbar hints contain ``/help``
  but never ``history``.

No LLM turn is driven — the popup is pure prompt_toolkit input-loop
behavior — but the mock LLM server env is injected exactly like the
other REPL e2e tests so the spawned CLI never reaches for real
credentials.

Usage::

    python -m pytest tests/e2e/test_repl_slash_completion_reopen_e2e.py -v --timeout=300
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pexpect = pytest.importorskip("pexpect")

_ASK_DEMO_DIR = Path(__file__).resolve().parents[1] / "resources" / "agents" / "ask-demo"

# Launch budget for ``omnigent run`` to reach an input-ready REPL —
# daemon spawn, local-server boot, agent upload, runner bring-up
# (see the rationale on _LAUNCH_TIMEOUT in test_repl_approval_e2e.py).
_LAUNCH_TIMEOUT = 120


@pytest.fixture(scope="module")
def repl_env(
    llm_api_key: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """
    Env for the spawned REPL — same shape as the other REPL e2e
    modules: fake HOME seeded with a persisted theme (skips the
    first-launch theme picker that would otherwise block the pty),
    ``auto_open_conversation: false``, onboarding skipped, and the
    inner OpenAI harness pointed at the mock LLM server.

    :param llm_api_key: ``"mock-key"`` in mock mode.
    :param mock_llm_server_url: Base URL of the mock LLM server.
    :param tmp_path_factory: Pytest temp-path factory for the fake HOME.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    real_databrickscfg = Path.home() / ".databrickscfg"
    fake_home = tmp_path_factory.mktemp("repl_home")
    config_home = fake_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n"
    )
    return {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "HOME": str(fake_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "DATABRICKS_CONFIG_FILE": str(real_databrickscfg),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }


@pytest.fixture(scope="module")
def ap_cli() -> str:
    """
    Resolved ``omnigent`` CLI path — prefers this venv's binary so a
    sibling editable install can't shadow the worktree under test.

    :returns: Absolute path to an executable.
    """
    venv_omnigent = Path(sys.executable).parent / "omnigent"
    if venv_omnigent.exists():
        return str(venv_omnigent)
    path = shutil.which("omnigent") or shutil.which("ap")
    if path is None:
        pytest.skip("Neither omnigent nor omnigent CLI on PATH")
    return path


def _wait_for_prompt_ready(child: Any, timeout: float) -> None:
    """
    Wait until the REPL is ready for input.

    The welcome banner renders before prompt_toolkit's input loop is
    live; the reliable input-readiness signal is the bottom status
    toolbar's ``· ready`` marker (see test_repl_approval_e2e.py).

    :param child: Active pexpect child.
    :param timeout: Max seconds to wait.
    """
    child.expect("ask.demo", timeout=timeout)
    child.expect(r"·\s*ready", timeout=timeout)


def _consume_output(child: Any, seconds: float) -> str:
    """
    Read and CONSUME everything the child emits during a window.

    Reads the pty fd directly (``read_nonblocking``) so the returned
    text is gone from the stream — a later call can only ever return
    output produced after this one, which is what lets the caller
    attribute menu renders to a specific phase of the journey.

    :param child: pexpect child.
    :param seconds: Total collection window.
    :returns: Raw output (ANSI sequences included) from the window.
    """
    deadline = time.monotonic() + seconds
    collected = ""
    while time.monotonic() < deadline:
        try:
            collected += child.read_nonblocking(size=65536, timeout=0.2)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
    return collected


def test_slash_completion_menu_reopens_after_backspace(
    ap_cli: str,
    repl_env: dict[str, str],
) -> None:
    """
    Deleting characters from a slash-command prefix must refresh the
    completion popup while the remaining prefix still has matches.

    Regression guard for "TUI slash-command suggestions do not reopen
    after deleting input": type ``/hel`` (popup opens for
    ``/help``), Backspace twice to ``/h``, and require the popup to
    reopen — detected by the ``/history`` completion row (marker
    ``history``, see module docstring) appearing in output emitted
    strictly after the deletions. On the buggy build nothing
    completion-shaped renders after Backspace, so this fails with the
    post-deletion output in the message.
    """
    child = pexpect.spawn(
        ap_cli,
        ["run", str(_ASK_DEMO_DIR)],
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),  # rows, cols — room for the popup below the input
        timeout=_LAUNCH_TIMEOUT,
    )
    try:
        _wait_for_prompt_ready(child, timeout=_LAUNCH_TIMEOUT)

        # Step 2 of the journey: type "/hel" one key at a time, as a
        # human does — each insertion lets complete_while_typing fire.
        for ch in "/hel":
            child.send(ch)
            time.sleep(0.2)

        # Precondition, not the regression assert: the popup opened
        # while typing. "Show this help" is the /help row's
        # description meta — it renders only inside the completion
        # menu (the toolbar hints include the bare "/help" string, so
        # that is not a usable menu marker). This window also settles
        # and discards every typing-phase render — including the
        # '/history' row shown transiently at the '/h' keystroke on
        # the way in — so the post-deletion window below can only
        # contain output caused by the deletions.
        typed_phase = _consume_output(child, seconds=3.0)
        assert "Show this help" in typed_phase, (
            "Completion popup never opened while typing '/hel' — the "
            "REPL harness itself is broken, cannot exercise the "
            f"deletion path.\nTyped-phase output:\n{typed_phase[-1500:]!r}"
        )

        # Steps 3–4: Backspace twice, leaving "/h".
        child.send("\x7f")
        time.sleep(0.3)
        child.send("\x7f")

        after_deletion = _consume_output(child, seconds=4.0)

        # The regression assert: the popup reopened for the "/h"
        # prefix. Only a completion-menu render for "/h" emits
        # "history" at this point in the journey (the /history row or
        # its meta); the buggy build emits only the shortened input
        # line and menu-region erasures.
        assert "history" in after_deletion, (
            "Completion popup did not reopen after Backspace left the "
            "valid prefix '/h' — '/help' and '/history' still match "
            "but no completion menu rendered.\n"
            f"Post-deletion output:\n{after_deletion[-1500:]!r}"
        )
    finally:
        try:
            child.send("\x03")  # Ctrl+C clears any partial input
            child.send("/quit\r")
            child.expect(pexpect.EOF, timeout=5)
        except Exception:
            pass
        if child.isalive():
            child.terminate(force=True)
