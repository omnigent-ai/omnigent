"""E2E — per-turn ``context_providers`` injection.

Runs ``tests/resources/examples/agent_with_context_provider.yaml`` through
``omnigent run`` and asserts the sentinel passphrase a declared context provider
injects into the system instructions appears in the model's reply — proving the
``context_providers`` hook augments the prompt on a real turn.

**What this proves:** the provider
(``tests.resources.examples._shared.context_provider_probe.remember_passphrase``)
returns a ``REMEMBER:`` line each turn; the runtime appends it to the system
instructions via ``build_instructions``'s ``per_request_instructions`` slot
(fed from ``AgentSpec.context_providers`` at the per-turn setup in
``runner/app.py``). The agent is told to echo the passphrase, so the sentinel in
stdout is end-to-end evidence the injection reached the model.

**What breaks if this fails:** the ``context_providers`` spec field / parser, the
single-file passthrough, ``runtime.context_providers.run_context_providers``, or
the per-turn wiring regressed — or the provider output stopped being appended.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which

import pytest

from tests.e2e._harness_probes import HARNESS_HARNESS_MODELS, HARNESS_IDS
from tests.resources.examples._shared.context_provider_probe import SENTINEL

_PROMPT = "What is the passphrase? Reply with it exactly and nothing else."
_RUN_TIMEOUT_SEC = 240


def _check_harness_available(harness: str, omnigent_python: Path) -> None:
    """Fail loud (don't silently skip) if the parametrized harness is missing."""
    if harness == "claude-sdk":
        probe = subprocess.run(
            [
                str(omnigent_python),
                "-c",
                "import importlib.util, sys; "
                "sys.exit(0 if importlib.util.find_spec('claude_agent_sdk') else 1)",
            ],
            capture_output=True,
        )
        if probe.returncode != 0 or which("claude") is None:
            pytest.fail(
                "claude-sdk harness prerequisites missing: both the "
                "'claude_agent_sdk' Python package and the 'claude' CLI binary "
                "must be present on PATH."
            )
    elif harness == "codex":
        if which("codex") is None:
            pytest.fail(
                "codex harness prerequisite missing: the 'codex' CLI binary must "
                "be installed on PATH (npm i -g @openai/codex)."
            )


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_yaml_context_provider_injects_passphrase(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    patched_databrickscfg: None,
    harness: str,
    model: str,
) -> None:
    """The injected REMEMBER line reaches the model: the reply contains the sentinel.

    cwd is the repo root so the YAML's ``context_providers`` dotted path
    (``tests.resources.examples._shared.context_provider_probe.remember_passphrase``)
    resolves on ``sys.path``.
    """
    _check_harness_available(harness, omnigent_python)
    yaml_path = (
        omnigent_repo_root
        / "tests"
        / "resources"
        / "examples"
        / "agent_with_context_provider.yaml"
    )

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
            model,
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=omnigent_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}.\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert SENTINEL in result.stdout, (
        f"Expected the context-provider sentinel {SENTINEL!r} in stdout — the "
        f"context_providers hook should have injected the REMEMBER line into the "
        f"system prompt.\n\nstdout:\n{result.stdout!r}"
    )
