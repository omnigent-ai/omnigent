"""Per-harness live characterization test — cursor harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness cursor -p "..."`` as a real
subprocess and asserts structural invariants (exit 0, a non-trivial assistant
reply). This is the end-to-end gate for the cursor harness: the full path
from CLI parse → spec materialize → spawn the ``cursor`` harness subprocess
→ :class:`CursorExecutor` driving ``cursor-agent --print --output-format
stream-json`` → stream-json parse → ``TurnComplete`` → the ``-p`` one-shot
printer.

**Prerequisite (skipped when absent):**
- The ``cursor-agent`` CLI on PATH (``curl https://cursor.com/install -fsS | bash``),
  authenticated via ``cursor-agent login`` or ``CURSOR_API_KEY``.

Unlike the other per-harness e2e tests, cursor-agent talks only to Cursor's own
backend and needs a Cursor account — there is no Databricks-gateway path, so
this test does NOT use ``patched_databrickscfg`` / ``omnigent_credentials_env``.
Because cursor-agent is an optional external dependency the Omnigent CI does not
provision, the test **skips** (rather than fails) when the binary is absent so
the e2e shards stay green; it runs for real wherever cursor-agent is installed
and authenticated (auth is via the ambient ``cursor-agent login`` / env, which
this run inherits, so a missing login surfaces as a real failure, not a skip).

**What breaks if this fails (with prerequisites present):**
- ``CursorExecutor`` regresses (subprocess orchestration, the stream-json →
  ExecutorEvent translation, session resume, or the system-prompt injection).
- The ``cursor-agent`` CLI changes its ``--print`` / ``--output-format
  stream-json`` contract or its event schema.
- ``omnigent.cli`` for the ``-p`` one-shot path stops printing assistant text
  to stdout on turn complete, or harness dispatch for ``cursor`` regresses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which

import pytest

_HARNESS = "cursor"
_PROMPT = "say hi in 5 words"

# Minimum assistant-text length. Anything longer than "hi" proves the turn
# produced a genuine model reply (not an empty response or an error banner).
_MIN_ASSISTANT_CHARS = 4

# cursor-agent cold-starts a session and round-trips to Cursor's backend; 180s
# matches the headroom the other coding-agent harnesses allow on CI hosts.
_RUN_TIMEOUT_SEC = 180


def test_per_harness_cursor_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
) -> None:
    """``omnigent run hello_world.yaml --harness cursor -p <prompt>`` works.

    :param omnigent_python: Interpreter with omnigent installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the YAML spec and
        example tool modules resolve on sys.path.
    """
    if which("cursor-agent") is None:
        pytest.skip(
            "cursor prerequisite missing: the 'cursor-agent' CLI is not on "
            "PATH (install via 'curl https://cursor.com/install -fsS | bash'). "
            "cursor-agent is an optional external dependency, so this live gate "
            "is skipped rather than failed when absent."
        )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    # cursor-agent needs PATH (to find its bundled runtime) and CURSOR_API_KEY /
    # ~/.cursor login from the ambient environment, so pass os.environ through.
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--harness",
            _HARNESS,
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=dict(os.environ),
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    assistant_text = result.stdout.strip()
    assert result.returncode == 0, (
        f"cursor run exited {result.returncode}.\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert len(assistant_text) >= _MIN_ASSISTANT_CHARS, (
        f"cursor assistant text shorter than {_MIN_ASSISTANT_CHARS} chars; "
        f"got {assistant_text!r}\n\nstderr:\n{result.stderr!r}"
    )
