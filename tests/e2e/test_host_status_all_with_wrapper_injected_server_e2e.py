"""The real ``omnigent`` entry point tolerates a wrapper-injected ``--server``.

A wrapper such as ``isaac omni`` sets ``OMNIGENT_REQUIRE_WRAPPER`` /
``OMNIGENT_WRAPPER_BYPASS`` / ``OMNIGENT_WRAPPER_COMMAND`` and rewrites
``isaac omni host status --all`` into ``omnigent host status --all --server
<managed>``. Driven as a subprocess so the wrapper guard in ``main()`` and Click
dispatch both run; the argument matrix lives in ``tests/host/test_cli_host.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_MANAGED_SERVER = "https://managed.example.databricksapps.com"

_AMBIENT_ENV_PREFIXES = ("OMNIGENT_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_", "DATABRICKS_")
_AMBIENT_ENV_KEYS = ("LLM_API_KEY", "CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION")

# Pinned onto PYTHONPATH so the child imports this checkout's omnigent.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _wrapper_env(fake_home: Path) -> dict[str, str]:
    """The env the ``isaac omni`` wrapper sets, rooted at an isolated ``$HOME``."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_AMBIENT_ENV_PREFIXES) and k not in _AMBIENT_ENV_KEYS
    }
    env["HOME"] = str(fake_home)
    # Pin both roots: HOME alone does not relocate Path.home() on Windows.
    env["OMNIGENT_DATA_DIR"] = str(fake_home / ".omnigent")
    env["OMNIGENT_CONFIG_HOME"] = str(fake_home / ".omnigent")
    env["LC_ALL"] = "C.UTF-8"
    env["TERM"] = "dumb"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    env["OMNIGENT_WRAPPER_COMMAND"] = "isaac omni"
    env["OMNIGENT_REQUIRE_WRAPPER"] = "1"
    env["OMNIGENT_WRAPPER_BYPASS"] = "1"
    return env


def test_wrapper_injected_server_reaches_host_status_through_the_real_entrypoint(
    tmp_path: Path,
) -> None:
    """The wrapper-rewritten ``host status --all`` renders the all-targets listing."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    proc = subprocess.run(
        [sys.executable, "-m", "omnigent", "host", "status", "--all", "--server", _MANAGED_SERVER],
        capture_output=True,
        text=True,
        timeout=120.0,
        env=_wrapper_env(fake_home),
        cwd=str(tmp_path),  # no project-level .omnigent config in scope
    )

    output = f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "Use either --server or --all, not both." not in output, (
        f"the injected --server tripped the mutual-exclusion guard.\n{output}"
    )
    assert proc.returncode == 0, (
        f"exit {proc.returncode} instead of rendering host status.\n{output}"
    )
    assert "No host daemons found." in proc.stdout, (
        f"expected the empty-registry listing.\n{output}"
    )
    assert "Ignoring --server: --all selects every known target." in proc.stderr, (
        f"the overridden --server must be reported on stderr.\n{output}"
    )
