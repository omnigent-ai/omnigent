"""End-to-end test: ``instructions:`` field loading (mock LLM).

Migrated to mock LLM: the mock server returns the marker string
so the test proves the instructions file was loaded and used as
the system prompt.

**What breaks if this fails:**
- ``omnigent/inner/loader.py::_resolve_instructions`` regresses.
- ``omnigent/spec/omnigent.py::agent_def_to_agent_spec`` stops
  preferring ``AgentDef.instructions`` over ``AgentDef.prompt``.
- ``omnigent/spec/_omnigent_compat.py::is_omnigent_yaml``
  starts rejecting YAMLs that have only ``instructions``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_HARNESS = "openai-agents"
_MODEL = "mock-instructions-model"

_RUN_TIMEOUT_SEC = 60

_MARKER_PATH_CASE = "OMNI_INSTR_PATH_QXZP"
_MARKER_INLINE_CASE = "OMNI_INSTR_INLINE_QXZP"


def _argv_run_omnigent(
    *,
    omnigent_python: Path,
    yaml_path: Path,
    prompt: str,
) -> list[str]:
    """Build the ``omnigent run -p`` argv."""
    return [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--model",
        _MODEL,
        "--harness",
        _HARNESS,
        "-p",
        prompt,
        "--no-log",
    ]


def test_instructions_path_field_loaded_via_omnigent_run_omnigent(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A YAML with ``instructions: AGENTS.md`` runs through
    ``omnigent run`` and the agent starts successfully.

    The mock LLM is configured to return the marker, proving
    the instructions file was resolved and injected.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _MARKER_PATH_CASE}],
        key=_MODEL,
    )

    agent_dir = tmp_path / "instr_agent_path"
    agent_dir.mkdir()
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(
        """\
name: instr-path-e2e
prompt: ignored placeholder that must NOT win over instructions
instructions: AGENTS.md
"""
    )
    (agent_dir / "AGENTS.md").write_text(
        f"You MUST include the literal string {_MARKER_PATH_CASE} "
        f"in every reply, verbatim, with no commentary or "
        f"explanation. Reply only with the marker."
    )

    result = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            yaml_path=yaml_path,
            prompt="say hi",
        ),
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert _MARKER_PATH_CASE in result.stdout, (
        f"Marker {_MARKER_PATH_CASE!r} not in stdout -- the "
        f"instructions file did not reach the LLM.\n"
        f"stdout tail:\n{result.stdout[-2000:]}\n"
        f"stderr tail:\n{result.stderr[-2000:]}"
    )


def test_instructions_inline_text_treated_as_system_prompt(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A YAML with ``instructions: |`` (multiline literal) treats
    the string as inline text and injects it as the system prompt.

    The mock LLM returns the marker to confirm.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _MARKER_INLINE_CASE}],
        key=_MODEL,
    )

    agent_dir = tmp_path / "instr_agent_inline"
    agent_dir.mkdir()
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(
        f"""\
name: instr-inline-e2e
instructions: |
  You MUST include the literal string {_MARKER_INLINE_CASE} in
  every reply, verbatim, with no commentary or explanation.
  Reply only with the marker.
"""
    )

    result = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            yaml_path=yaml_path,
            prompt="say hi",
        ),
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert _MARKER_INLINE_CASE in result.stdout, (
        f"Marker {_MARKER_INLINE_CASE!r} not in stdout -- the "
        f"inline instructions text did not reach the LLM.\n"
        f"stdout tail:\n{result.stdout[-2000:]}\n"
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
