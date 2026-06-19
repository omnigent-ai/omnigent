"""End-to-end: ``omnigent run`` renders through sessions (mock LLM).

Migrated to mock LLM: uses the mock server for the LLM response
so the test is deterministic and needs no Databricks credentials.

Driven under a pseudo-terminal via :mod:`pexpect` because the
REPL's TUI requires a TTY to render.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pexpect
import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env, submit_prompt

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODEL = "mock-sessions-default-model"
_MARKER = "MARKER_SESSIONS_DEFAULT_42"
_REPL_TIMEOUT_S = 60
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI_RE.sub("", text)


def _spawn_repl(
    yaml_path: Path,
    env: dict[str, str],
) -> pexpect.spawn:
    """Spawn ``omnigent run`` under a PTY."""
    spawn_env = {
        **env,
        "TERM": "xterm-256color",
        "LINES": "40",
        "COLUMNS": "120",
    }
    spawn_env = ensure_repl_test_theme_env(spawn_env)
    return pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            _MODEL,
        ],
        env=spawn_env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        timeout=_REPL_TIMEOUT_S,
        dimensions=(40, 120),
    )


def test_repl_default_sessions_renders_assistant_text(
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """``omnigent run`` renders assistant text through sessions.

    Drives the REPL through a PTY: types the prompt, waits for the
    marker to appear on stdout, then exits via Ctrl+D.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _MARKER}],
        key=_MODEL,
    )

    yaml_path = tmp_path / "hello_world_marker.yaml"
    yaml_path.write_text(
        f"name: hello_world_marker\n"
        f"executor:\n"
        f"  harness: openai-agents\n"
        f"  model: {_MODEL}\n"
        f"prompt: |\n"
        f"  You MUST reply with exactly the literal string\n"
        f"  {_MARKER}\n"
        f"  and nothing else.\n",
    )

    child = _spawn_repl(yaml_path=yaml_path, env=mock_credentials_env)
    try:
        child.expect("\u276f", timeout=60)
        submit_prompt(child, "Say the marker.")
        try:
            child.expect(_MARKER, timeout=_REPL_TIMEOUT_S)
        except pexpect.TIMEOUT:
            before = child.before if isinstance(child.before, str) else ""
            after = child.after if isinstance(child.after, str) else ""
            buf = _strip_ansi(before + after)
            pytest.fail(
                f"Marker {_MARKER!r} never appeared. The REPL likely "
                f"crashed during stream rendering.\n\n"
                f"PTY buffer (last 4 KB, ANSI stripped):\n"
                f"{buf[-4096:]}"
            )
    finally:
        try:
            child.sendcontrol("d")
            child.expect(pexpect.EOF, timeout=10)
        except pexpect.ExceptionPexpect:
            pass
        child.close(force=True)
