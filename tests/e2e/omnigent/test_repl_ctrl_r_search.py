"""Phase 0 characterization test -- Ctrl+R reverse-incremental search (mock LLM).

Migrated to mock LLM: uses canned responses for the LLM turns
so the test is deterministic.

**What breaks if this fails:**
- ``omnigent.cli`` removes the ``@kb.add("c-r")`` binding.
- ``omnigent.cli`` forgets to bind Enter while searching.
- ``SearchToolbar`` stops rendering its default prompt.
- The input-area buffer's history search loses submitted prompts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for
from tests.e2e.omnigent._snapshot import compare_snapshot

_MODEL = "mock-ctrl-r-model"
_HARNESS = "openai-agents"

_NEEDLE = "zxqw-unique-history-token"
_PROMPT = f"please just say ok ({_NEEDLE})"

_SEARCH_PROMPT_MARKER = "I-search backward"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 30.0
_RUNNING_TIMEOUT = 20.0
_COMPLETION_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0

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
    """
    reset_mock_llm(mock_llm_server_url)
    # Two turns: the initial prompt and the re-submitted prompt via Ctrl+R
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "ok"},
            {"text": "ok again"},
        ],
        key=_MODEL,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    env = dict(mock_credentials_env)
    env["PYTHONPATH"] = f"{omnigent_repo_root}:{omnigent_repo_root / 'sdks' / 'python-client'}" + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(r"\u276f ", timeout=_BOOT_TIMEOUT)
        submit_prompt(child, _PROMPT)
        await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_COMPLETION_TIMEOUT,
            running_marker=r"working",
            completion_pattern=r"\u276f ",
        )
        child.sendcontrol("r")
        child.send(_NEEDLE)
        search_drain = drain_for(child, _SEARCH_DRAIN_TIMEOUT)

        child.send("\r")
        accept_drain = drain_for(child, _ACCEPT_DRAIN_TIMEOUT)
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
                completion_pattern=r"\u276f ",
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
        "search_toolbar_visible": _SEARCH_PROMPT_MARKER in combined_stripped,
        "needle_surfaced": _NEEDLE in search_stripped,
        "enter_accepts_search": accepted_search_submits,
    }
    diffs = compare_snapshot("test_repl_ctrl_r_search", observed)
    assert diffs == [], (
        "Snapshot mismatch for Ctrl+R reverse-search:\n"
        + "\n".join(diffs)
        + f"\n\nsearch-drain stripped (last 2000):\n"
        f"{combined_stripped[-2000:]}"
    )
