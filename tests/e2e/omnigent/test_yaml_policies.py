"""Phase 0 characterization test -- ``agent_with_policies.yaml`` (mock LLM).

Migrated to mock LLM. The policy engine's input classifier runs a
separate LLM call (through the policy's ``executor``), so the mock
must serve TWO responses: one for the policy judge (returning a DENY
verdict JSON) and one for the base model (which should never be
reached because the judge denies first).

The policy YAML pins the executor model for the ``block_canada_input``
policy, and the base model is passed via ``--model``. We configure
separate keyed queues so each model gets its own response.

**What breaks if this fails:**
- Omnigent' policy engine regresses.
- YAML spec parsing regresses on the ``policies:`` block.
- The prompt-policy evaluator drops the ``reason`` field.
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

_PROMPT = "Name the provinces of Canada."

_DENIED_MARKER = "[Denied by policy: Canada-related topics are denied"

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
def test_yaml_policies_blocks_canada_input(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    harness: str,
    model: str,
) -> None:
    """
    ``omnigent run agent_with_policies.yaml --harness <harness>
    -p "Name the provinces of Canada."`` exits 0 and stdout
    contains the denial marker.

    Parametrized across every wrapped harness so the policy engine
    is verified end-to-end once per harness.

    The mock LLM is configured to return a DENY verdict for the
    policy judge model. The base model queue is also configured
    but should never be consumed.

    :param harness: The harness identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    :param model: Unused in mock mode — replaced by a per-harness
        mock key so each row gets an isolated response queue.
    """
    del model  # replaced by base_model below
    skip_if_harness_cli_missing(harness)

    base_model = f"mock-policy-base-{harness}"
    reset_mock_llm(mock_llm_server_url)

    yaml_path = (
        omnigent_repo_root / "tests" / "resources" / "examples" / "agent_with_policies.yaml"
    )

    # The policy executor model is set in the YAML. Rather than
    # parsing it, use the "default" queue which catches any model
    # not explicitly keyed. The policy judge is called first and
    # consumes the first default-queue response.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": '{"action": "DENY", "reason": "Canada-related topics are denied."}',
            },
        ],
    )
    # Base model queue (should not be reached, but configure to
    # avoid a 500 if the deny path regresses).
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "This should not be reached."}],
        key=base_model,
    )

    env = _build_harness_env(harness, mock_credentials_env, mock_llm_server_url)

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--harness",
            harness,
            "--model",
            base_model,
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
        stdin=subprocess.DEVNULL,
    )

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stdout": result.stdout,
    }

    diffs = compare_snapshot("test_yaml_policies", observed)
    assert diffs == [], (
        "Snapshot mismatch for agent_with_policies run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert _DENIED_MARKER in result.stdout, (
        f"Expected policy-denial marker {_DENIED_MARKER!r} in "
        f"stdout -- ``block_canada_input`` should have blocked "
        f"the prompt.\n\nstdout:\n{result.stdout!r}"
    )
