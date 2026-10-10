"""Fork/prompt conflicts are rejected before local or server dispatch, without a traceback.

``omnigent run --fork <id> -p ...`` must print the same one-line usage error for
a local YAML target and for ``--server <url>``. Both shapes run the real CLI as
a subprocess so the whole process is observed (exit code, no traceback, no crash
reporter), not just the Click command. The guard fires before any connection,
so no server is started; the ``--server`` URL points at a closed loopback port.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.e2e.conftest import find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELLO_WORLD_YAML = _REPO_ROOT / "tests" / "resources" / "examples" / "hello_world.yaml"

# Substring of the guard message so it survives minor phrasing tweaks around it.
_GUARD_MSG = "--fork requires interactive REPL mode"

# A usage error is reported cleanly; a traceback means dispatch was reached.
_TRACEBACK = "Traceback (most recent call last)"

# The guard fires during argument validation, so the id is never dereferenced.
_DUMMY_SESSION_ID = "0" * 32


def _isolated_env(root: Path) -> dict[str, str]:
    """Env for an ``omnigent`` subprocess with a no-auth config and its own data dir.

    The ambient ``PYTHONPATH`` is kept (its relative entries resolve against
    ``cwd=_REPO_ROOT``) so the subprocess imports this worktree's packages.
    """
    config_home = root / "omnigent-config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text("auth:\n  type: none\n", encoding="utf-8")
    env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(root / "omnigent-data"),
        "OPENAI_API_KEY": "mock-key",
    }
    env.pop("DATABRICKS_TOKEN", None)
    return env


def _run_cli(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run ``omnigent <args>`` and return ``(exit_code, stdout + stderr)``."""
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.cli", *args],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _assert_guarded(code: int, out: str) -> None:
    assert code != 0, out
    assert _GUARD_MSG in out, out
    assert _TRACEBACK not in out, out


def test_fork_prompt_local_yaml_target_is_guarded(tmp_path: Path) -> None:
    """Control: the local-YAML shape rejects ``--fork`` + ``-p`` before any backend work."""
    code, out = _run_cli(
        ["run", str(_HELLO_WORLD_YAML), "--fork", _DUMMY_SESSION_ID, "-p", "test"],
        _isolated_env(tmp_path),
    )
    _assert_guarded(code, out)


def test_fork_prompt_server_url_target_is_guarded(tmp_path: Path) -> None:
    """The ``--server <url>`` shape prints the same guard instead of dispatching."""
    server_url = f"http://127.0.0.1:{find_free_port()}"
    code, out = _run_cli(
        ["run", "--server", server_url, "--fork", _DUMMY_SESSION_ID, "-p", "test"],
        _isolated_env(tmp_path),
    )
    _assert_guarded(code, out)
