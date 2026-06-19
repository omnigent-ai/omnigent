"""E2e compaction test for the sessions-native path.

Uses pexpect to run multiple turns within a single ``omnigent run``
session. With ``AP_CONTEXT_WINDOW_OVERRIDE=256`` the compaction budget
(``0.8 * window`` ≈ 204 tokens) is tiny, so proactive compaction fires
after the first verbose turn. The override is a runner-side budget knob
only; it does not cap the harness API call, so the model still produces
full verbose replies that grow the persisted history past the budget.

Boot/turn synchronization and auth go through the shared
``_pexpect_harness`` helpers (the same path every green REPL e2e test
uses):

- ``spawn_omnigent_run`` seeds a TUI theme so the first-run interactive
  theme picker (which blocks on raw keypresses a pexpect child never
  sends) doesn't sit in front of the REPL prompt, and symlinks the
  Databricks auth files into the isolated test HOME so the openai-agents
  harness authenticates (without this it 401s with "Invalid Token" /
  "Credential was not sent" and never produces an assistant turn, so
  history never grows and compaction can't fire).
- ``wait_for_ready`` / ``await_turn_complete`` match the visible ``❯``
  prompt and ``working`` activity line rather than the bottom-toolbar
  ``state: sleeping`` badge, which prompt-toolkit fragments across
  CPR/cursor-move sequences under a PTY (a literal ``sleeping`` substring
  never appears, so a naive wait hangs until the boot timeout).

``OMNIGENT_DATA_DIR`` isolates the runtime data dir (``chat.db`` + the
per-test local server) so the test can inspect the persisted compaction
item without touching the developer's ``~/.omnigent``.

Run with::

    pytest tests/e2e/omnigent/test_compaction_sessions_native_e2e.py -v --profile oss
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tests._model_pools import resolve_model
from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
    wait_for_ready,
)

_COMPACTION_AGENT_YAML = """\
name: compaction-e2e-test
description: Agent for e2e compaction testing.

executor:
  harness: openai-agents

prompt: |
  You are a test assistant. Reply with detailed, verbose answers
  so that conversation history grows quickly.
"""

_MODEL = resolve_model("databricks-gpt-5-4-mini", key=__name__)
_HARNESS = "openai-agents"
_BOOT_TIMEOUT = 120.0
_RUNNING_TIMEOUT = 30.0
_TURN_TIMEOUT = 300.0
_EXIT_TIMEOUT = 20.0

# Visible turn-synchronization markers (see _pexpect_harness and
# test_repl_history_recall for the rationale). ``working`` is the
# spinner activity line printed while a turn runs; ``❯ `` is the idle
# input prompt the REPL returns to when the turn completes.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "


def test_compaction_fires_and_agent_retains_context(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Multi-turn pexpect test: 2 verbose turns trigger proactive
    compaction, then a 3rd turn proves the agent retains context.

    Breakage this catches: if proactive compaction doesn't fire,
    the compaction item won't appear in the DB. If the summary
    doesn't capture prior context, turn 3 can't reference it.
    """
    yaml_path = tmp_path / "compaction-e2e-test.yaml"
    yaml_path.write_text(_COMPACTION_AGENT_YAML)
    # Isolated runtime data dir: chat.db and the per-test local server's
    # pidfile both resolve under here (omnigent.host.local_server
    # ._local_data_dir honors OMNIGENT_DATA_DIR), so the test inspects
    # its own DB and gets a fresh server instead of reusing a shared one.
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    env = dict(omnigent_credentials_env)
    # Tiny context window so the compaction budget (0.8 * window ≈ 51
    # tokens) is exceeded by the very first turn's history — even the
    # user prompt alone is ~75 tokens. A larger budget (the old 256 →
    # 204) made firing depend on how verbose the model happened to be
    # that run, which flaked ~40% of CI attempts. This is a runner-side
    # budget knob only; it does not cap the harness API call, so the
    # model still produces full replies for the context-retention check.
    env["AP_CONTEXT_WINDOW_OVERRIDE"] = "64"
    env["OMNIGENT_DATA_DIR"] = str(data_dir)

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_TURN_TIMEOUT,
        no_log=True,
        # Keep sessions on: the test asserts on the persisted chat.db,
        # which only the sessions path writes.
        no_session=False,
    )
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)

        submit_prompt(
            child,
            (
                "List exactly 20 countries. For each country, write the capital city, "
                "the population, the official language, the currency, and a famous "
                "landmark with a 3-sentence description. Number them 1 through 20."
            ),
        )
        turn1 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        assert len(turn1.stripped) > 100, f"Turn 1 too short: {turn1.stripped[:100]!r}"

        submit_prompt(
            child,
            (
                "Now list 20 MORE countries not in the previous list, same detailed "
                "format with capital, population, language, currency, and landmark."
            ),
        )
        turn2 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        assert len(turn2.stripped) > 100, f"Turn 2 too short: {turn2.stripped[:100]!r}"

        submit_prompt(
            child,
            "What was the very first thing I asked you? Reply in one sentence.",
        )
        turn3 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )

        # Wait for the server's relay to persist items before exit.
        time.sleep(5)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    # Verify compaction item was persisted to the DB.
    db_path = data_dir / "chat.db"
    assert db_path.is_file(), f"DB not found at {db_path}"
    with sqlite3.connect(str(db_path)) as conn:
        compaction_rows = conn.execute(
            "SELECT type FROM conversation_items WHERE type = 'compaction'"
        ).fetchall()
    # At least 1 compaction item: proactive compaction fired after a
    # verbose turn's history exceeded the tiny token budget. 0 means
    # _proactive_compact_if_needed didn't fire or the POST to the
    # server didn't persist the item.
    assert len(compaction_rows) >= 1, (
        f"Expected >= 1 compaction item in DB. Found {len(compaction_rows)}."
    )

    # Verify turn 3 references prior context — proves the
    # compacted summary preserved meaningful context.
    combined = turn3.stripped.lower()
    assert any(
        kw in combined for kw in ["countr", "capital", "landmark", "list", "nation", "asked"]
    ), f"Turn 3 doesn't reference prior context. Response: {turn3.stripped[:300]!r}"
