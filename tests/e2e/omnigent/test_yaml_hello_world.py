"""Phase 0 characterization test — YAML-driven agent with tools (mock LLM).

Migrated to mock LLM: uses the session-scoped mock server instead of
a real Databricks gateway.  Parametrized across every wrapped harness
(claude-sdk, codex, pi, openai-agents); rows whose harness CLI binary
is absent are skipped via :func:`skip_if_harness_cli_missing` so CI
still picks up openai-agents without needing the ``claude`` / ``codex``
/ ``pi`` binaries.

**What breaks if this fails:**
- Omnigent' YAML spec parser regresses on ``tools.*`` entries
  (``function`` / ``cancellable_function`` types).
- The wrapped harness loses its MCP tool bridging or its
  prompt-construction path.
- Per-YAML defaults fail to pick up the ``callable:`` dotted
  paths via ``importlib.import_module`` — the tool never gets
  registered and the agent can't invoke it.
- ``omnigent.cli`` one-shot path stops streaming tool-call
  lifecycle lines (``◦ <tool>`` / ``• <tool>``) to stdout.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
YAML→agent characterization.
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

_PROMPT = "What is 3 + 4? Use the calculate tool."

# ``agent_with_tools.yaml`` defines a ``calculate`` tool. The
# REPL's tool-lifecycle lines look like
# ``◦ calculate`` (start) and ``• calculate (NNms)`` (complete).
# We snapshot the substring ``"calculate"`` so the comparator
# succeeds as long as either line appears, regardless of the
# exact timing format.
_EXPECTED_TOOL_NAME = "calculate"

# Minimum assistant-text length. The prompt asks a direct
# arithmetic question so the reply is typically short but must
# be longer than e.g. "7" to prove the full turn streamed.
_MIN_ASSISTANT_CHARS = 3

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
        # The Claude SDK reads ANTHROPIC_BASE_URL; it appends
        # /v1/messages itself, so we pass the raw server root.
        env["ANTHROPIC_BASE_URL"] = mock_url
        # API key helper: a shell command the Claude CLI runs to get
        # a bearer token.  ``printf %s mock-key`` echoes the literal
        # string without a trailing newline, matching what the SDK
        # expects from a helper command.
        env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] = "printf %s mock-key"
        # Remove OPENAI_* vars so the SDK doesn't accidentally try
        # to hit an OpenAI endpoint.
        env.pop("OPENAI_BASE_URL", None)
        env.pop("OPENAI_API_KEY", None)
    # codex, openai-agents, and pi all speak the OpenAI Responses
    # API; OPENAI_BASE_URL / OPENAI_API_KEY from base_env are
    # already correct for these harnesses.
    return env


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_yaml_agent_with_tools(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    harness: str,
    model: str,
) -> None:
    """
    Running ``omnigent run agent_with_tools.yaml --harness
    <harness> -p <calc-prompt>`` completes cleanly and the
    ``calculate`` tool appears in stdout.

    Parametrized across every wrapped harness (claude-sdk, codex,
    pi, openai-agents) so the YAML→tools pipeline is verified
    end-to-end once per harness.  Rows whose CLI binary is absent
    are skipped via :func:`skip_if_harness_cli_missing`.

    Uses the mock LLM server to provide a canned tool-call then
    text response so the test is deterministic and requires no
    real credentials.

    :param omnigent_python: Interpreter with omnigent +
        the harness's SDK installed.
    :param omnigent_repo_root: Cwd for the subprocess — the
        YAML's ``callable:`` entries
        (``tests.resources.examples._shared.tool_functions.calculate``)
        only import if the repo root is on sys.path, which
        ``cwd=...`` achieves.
    :param mock_credentials_env: Env vars pointing at the mock
        LLM server (``OPENAI_BASE_URL`` + ``OPENAI_API_KEY``).
    :param mock_llm_server_url: Base URL of the mock LLM server.
    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: Unused in mock mode — the real model name from
        :data:`HARNESS_HARNESS_MODELS` is replaced by a
        per-harness mock key so each row gets an isolated response
        queue on the mock server.
    """
    del model  # replaced by mock_model below
    skip_if_harness_cli_missing(harness)

    # Use a per-harness model key so concurrent harness rows get
    # isolated mock response queues (no cross-contamination).
    mock_model = f"mock-calc-{harness}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_calc_1",
                        "name": "calculate",
                        "arguments": '{"expression": "3 + 4"}',
                    }
                ],
            },
            {"text": "The answer is 7."},
        ],
        key=mock_model,
    )

    env = _build_harness_env(harness, mock_credentials_env, mock_llm_server_url)
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "agent_with_tools.yaml"

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
        # Combined stdout because the tool-lifecycle lines
        # (``◦ calculate`` / ``• calculate``) and the assistant
        # reply both land on stdout, not stderr. The snapshot's
        # ``contains`` comparator checks for the tool name.
        "stdout": result.stdout,
        "stderr_is_clean": result.stderr.strip() == "",
    }

    diffs = compare_snapshot("test_yaml_hello_world", observed)
    assert diffs == [], (
        "Snapshot mismatch for agent_with_tools run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )

    # Length check on the *assistant* portion, not the
    # tool-lifecycle lines. We strip the known banner
    # prefix to isolate the reply.
    stripped = _strip_tool_chatter(result.stdout)
    assert len(stripped) >= _MIN_ASSISTANT_CHARS, (
        f"Assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars after stripping tool lifecycle lines; got "
        f"{stripped!r} (full stdout: {result.stdout!r})"
    )
    # Belt-and-braces — the snapshot's ``contains`` comparator
    # already covers this, but naming the assertion explicitly
    # makes the failure message self-explanatory if the
    # snapshot file is ever accidentally deleted.
    assert _EXPECTED_TOOL_NAME in result.stdout, (
        f"Expected tool name {_EXPECTED_TOOL_NAME!r} not found "
        f"in stdout; the {harness} harness did not invoke "
        f"the calculate tool.\n\nstdout:\n{result.stdout!r}"
    )


def _strip_tool_chatter(stdout: str) -> str:
    """
    Remove known tool-lifecycle marker lines from stdout.

    The omnigent CLI prints ``◦ <tool>`` (queued) and
    ``• <tool> (NNms)`` (done) lines around tool calls regardless
    of which harness fired them. For the assistant-length
    assertion we want to measure only the natural-language reply,
    not those markers.

    :param stdout: Raw stdout from ``omnigent run``.
    :returns: The stdout with tool lifecycle lines removed,
        trimmed of leading/trailing whitespace.
    """
    kept: list[str] = []
    for line in stdout.splitlines():
        stripped_line = line.strip()
        # Both markers use exotic unicode glyphs not likely to
        # appear in an arithmetic reply, so prefix-matching
        # them is safe.
        if stripped_line.startswith(("\u25e6 ", "\u2022 ")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()
