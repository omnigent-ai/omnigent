"""E2E tests for ``omnigent config --global`` defaults (mock LLM).

Migrated to mock LLM: tests 1 and 2 never used LLM (config commands
only). Test 3 uses ``omnigent run`` with a mock model so no real
credentials are needed.

Unit-level coverage of the config command lives in
``tests/cli/test_cli.py``. These tests close the subprocess boundary
gap: does one ``omnigent config`` invocation write a file that the
next invocation reads back correctly?
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_RUN_TIMEOUT_SEC = 60


def _bare_env(home: Path, omnigent_repo_root: Path) -> dict[str, str]:
    """
    Build a minimal subprocess env that doesn't need LLM credentials.

    :param home: Directory to use as ``$HOME``.
    :param omnigent_repo_root: Worktree root, prepended onto
        ``PYTHONPATH``.
    :returns: Env dict suitable for ``subprocess.run(env=...)``.
    """
    existing_pp = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join(p for p in (str(omnigent_repo_root), existing_pp) if p)
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": pythonpath,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }


def _run_omnigent(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    env: dict[str, str],
    args: list[str],
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m omnigent <args>`` with the given env."""
    return subprocess.run(
        [str(omnigent_python), "-m", "omnigent", *args],
        env=env,
        cwd=str(omnigent_repo_root),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def test_global_config_write_then_list_roundtrips(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """
    ``config set --global KEY=VALUE`` writes the file; ``config list``
    reads it back. ``config unset`` removes the key.

    No LLM needed -- only config file I/O.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _bare_env(home, omnigent_repo_root)

    write = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=[
            "config",
            "set",
            "--global",
            "default_agent=tests/resources/examples/hello_world.yaml",
            "model=databricks-claude-sonnet-4-6",
            "server=https://example.databricks.com",
        ],
    )
    assert write.returncode == 0, (
        f"config set --global write failed: stdout={write.stdout!r} stderr={write.stderr!r}"
    )

    config_path = home / ".omnigent" / "config.yaml"
    assert config_path.is_file(), f"Expected config at {config_path} after write; not found."

    listed = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=["config", "list"],
    )
    assert listed.returncode == 0, (
        f"config list failed: stdout={listed.stdout!r} stderr={listed.stderr!r}"
    )
    assert "model=databricks-claude-sonnet-4-6" in listed.stdout, (
        f"model not in --list output; got {listed.stdout!r}"
    )
    assert "server=https://example.databricks.com" in listed.stdout, (
        f"server not in --list output; got {listed.stdout!r}"
    )
    assert "default_agent=" in listed.stdout, (
        f"default_agent not in --list output; got {listed.stdout!r}"
    )
    assert "hello_world.yaml" in listed.stdout, (
        f"hello_world.yaml not in --list output; got {listed.stdout!r}"
    )

    unset = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=["config", "unset", "--global", "server"],
    )
    assert unset.returncode == 0, (
        f"config unset failed: stdout={unset.stdout!r} stderr={unset.stderr!r}"
    )

    listed_after = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=["config", "list"],
    )
    assert listed_after.returncode == 0
    assert "server=" not in listed_after.stdout, (
        f"server key should be gone after unset; got {listed_after.stdout!r}"
    )
    assert "model=databricks-claude-sonnet-4-6" in listed_after.stdout
    assert "default_agent=" in listed_after.stdout


def test_global_config_unknown_key_rejected_at_subprocess_boundary(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """
    ``config set --global bogus_key=foo`` exits non-zero.

    No LLM needed -- only config validation.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _bare_env(home, omnigent_repo_root)

    result = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=["config", "set", "--global", "bogus_key=foo"],
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit for unknown config key; got 0.\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "bogus_key" in combined, (
        f"Expected the unknown key name in the error message; "
        f"got stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    config_path = home / ".omnigent" / "config.yaml"
    assert not config_path.exists() or "bogus_key" not in config_path.read_text(), (
        f"Invalid key was persisted to {config_path}; write should "
        f"have been rejected before touching the file."
    )


def test_global_config_default_agent_drives_bare_omnigent(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A ``default_agent`` set via ``config set --global`` is honored by
    bare ``omnigent -p ...`` (no AGENT arg).

    Uses mock LLM so the run path exercises the full pipeline
    without real credentials.
    """
    model = "mock-config-default-model"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from config default agent!"}],
        key=model,
    )

    home = tmp_path / "home"
    home.mkdir()
    env = dict(mock_credentials_env)
    env["HOME"] = str(home)

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    write = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=[
            "config",
            "set",
            "--global",
            f"default_agent={yaml_path}",
            "harness=openai-agents",
            f"model={model}",
        ],
    )
    assert write.returncode == 0, (
        f"config set --global write failed: stdout={write.stdout!r} stderr={write.stderr!r}"
    )

    run = _run_omnigent(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        env=env,
        args=["run", "-p", "say hi in 5 words", "--no-session", "--no-log"],
    )
    assert run.returncode == 0, (
        f"bare ``omnigent run`` with global default_agent failed: "
        f"stdout={run.stdout!r} stderr={run.stderr!r}"
    )
    assert len(run.stdout.strip()) >= 4, (
        f"Expected an assistant reply in stdout; got {run.stdout!r}"
    )
