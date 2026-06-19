"""Per-harness live characterization test — cursor harness, one-shot prompt.

Runs ``omnigent run hello_world.yaml --harness cursor -p "..."`` as a real
subprocess and asserts structural invariants (exit 0, a non-trivial assistant
reply). This is the end-to-end gate for the cursor harness: the full path
from CLI parse → spec materialize → spawn the ``cursor`` harness subprocess
→ :class:`CursorExecutor` driving a persistent Cursor SDK (``cursor-sdk``) agent
over a local bridge (``agent.send`` → streamed ``run.messages()``) →
``TurnComplete`` → the ``-p`` one-shot printer.

**Prerequisites (skipped when absent):**
- The ``cursor-sdk`` package installed (a baseline dependency).
- ``CURSOR_API_KEY`` set — the SDK requires an API key and does NOT reuse a
  ``cursor-agent login``. The subprocess receives this key with surrounding
  newlines to prove the CLI/spawn-env path strips env-detected keys before
  forwarding them to the cursor SDK (F103).

Unlike the other per-harness e2e tests, the Cursor SDK talks only to Cursor's
own backend — there is no Databricks-gateway path, so this test does NOT use
``patched_databrickscfg`` / ``omnigent_credentials_env``. Because a Cursor API
key is not provisioned on CI, the test **skips** (rather than fails) when
``CURSOR_API_KEY`` is absent so the e2e shards stay green; it runs for real
wherever a key is present.

**Why this test cannot use the mock LLM server:** The ``cursor-sdk`` connects
directly to Cursor's proprietary backend using ``CURSOR_API_KEY`` — it does not
honour ``OPENAI_BASE_URL`` the way the ``openai-agents`` harness does. There is
no OpenAI-compatible shim path in the Cursor SDK, so pointing
``OPENAI_BASE_URL`` at the mock server has no effect. This harness can only be
exercised with a real Cursor API key; the ``pytest.skip`` below gates the test
cleanly when the key is absent.

**What breaks if this fails (with prerequisites present):**
- ``CursorExecutor`` regresses (the ``SDKMessage`` → ExecutorEvent translation,
  the ``custom_tools`` tool bridge, persistent-agent reuse, or the system-prompt
  injection).
- The ``cursor-sdk`` API contract changes (``AsyncAgent`` / ``AsyncClient`` /
  ``run.messages()`` shape).
- ``omnigent.cli`` for the ``-p`` one-shot path stops printing assistant text
  to stdout on turn complete, or harness dispatch for ``cursor`` regresses.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

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
    if importlib.util.find_spec("cursor_sdk") is None:
        pytest.skip("cursor prerequisite missing: the 'cursor-sdk' package is not installed.")
    cursor_api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not cursor_api_key:
        pytest.skip(
            "cursor prerequisite missing: CURSOR_API_KEY is not set. The Cursor SDK "
            "requires an API key (it does not reuse a 'cursor-agent login'), so this "
            "live gate is skipped rather than failed when the key is absent."
        )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    env = dict(os.environ)
    # Regression coverage for F103: this live gate runs the real
    # CLI -> spawn-env -> cursor-harness path, so pad the key in the same shape
    # that previously reached the SDK verbatim and failed auth.
    env["CURSOR_API_KEY"] = f"\n{cursor_api_key}\n"
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
        env=env,
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
