"""Phase 0 characterization test -- ``hello_world.yaml`` end-to-end (mock LLM).

Migrated to mock LLM: uses a canned text response so the test is
deterministic and needs no real credentials.

**What breaks if this fails:**
- Omnigent' YAML spec parser regresses on the minimal
  ``name:`` + ``prompt:`` shape.
- ``omnigent.loader`` stops applying CLI ``--model`` as a
  fallback when the YAML omits ``executor.model``.
- The default harness selection path regresses.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing the assistant text on turn complete.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import (
    HARNESS_HARNESS_MODELS,
    HARNESS_IDS,
    skip_if_harness_cli_missing,
)
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._snapshot import compare_snapshot

_PROMPT = "say hi in 5 words"

_MIN_ASSISTANT_CHARS = 4

_RUN_TIMEOUT_SEC = 60


def _build_harness_env(
    harness: str,
    base_env: dict[str, str],
    mock_url: str,
) -> dict[str, str]:
    """
    Overlay harness-specific mock-server routing onto ``base_env``.

    ``base_env`` (from :func:`mock_credentials_env`) already has
    ``OPENAI_BASE_URL`` pointed at ``<mock_url>/v1`` and
    ``OPENAI_API_KEY=mock-key``, which is correct for the
    openai-agents, codex, and pi harnesses.  claude-sdk speaks the
    Anthropic Messages API instead, so we swap in
    ``ANTHROPIC_BASE_URL`` (the SDK appends ``/v1/messages``) and
    set the API-key helper so the CLI resolves a bearer token
    without hitting a real Anthropic endpoint.

    :param harness: Harness identifier, e.g. ``"claude-sdk"``.
    :param base_env: Env dict from :func:`mock_credentials_env`.
    :param mock_url: Base URL of the session-scoped mock server,
        e.g. ``"http://127.0.0.1:12345"``.
    :returns: A shallow copy of ``base_env`` with the per-harness
        overrides applied.
    """
    env = dict(base_env)
    if harness == "claude-sdk":
        env["ANTHROPIC_BASE_URL"] = mock_url
        env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] = "printf %s mock-key"
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
    return env


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_yaml_hello_world_real(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    harness: str,
    model: str,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness <harness> --model
    <model> -p <prompt>`` exits 0 and emits a non-trivial
    assistant reply using the mock LLM.

    Parametrized across every wrapped harness so the minimal YAML
    spec path is verified end-to-end once per harness.

    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: Unused in mock mode — replaced by a per-harness
        mock key so each row gets an isolated response queue.
    """
    del model  # replaced by mock_model below
    skip_if_harness_cli_missing(harness)

    mock_model = f"mock-hello-{harness}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there nice to meet!"}],
        key=mock_model,
    )

    env = _build_harness_env(harness, mock_credentials_env, mock_llm_server_url)
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            mock_model,
            "--harness",
            harness,
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

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stderr_is_clean": result.stderr.strip() == "",
        "assistant_text": result.stdout.strip(),
    }

    diffs = compare_snapshot("test_yaml_hello_world_real", observed)
    assert diffs == [], (
        "Snapshot mismatch for hello_world.yaml run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"hello_world assistant text shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got "
        f"{observed['assistant_text']!r}"
    )
