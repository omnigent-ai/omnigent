"""CLI startup must survive an interpreter whose ``pyexpat`` cannot load.

``omnigent claude`` crashed at startup on macOS under a Homebrew Python 3.14
whose ``pyexpat`` extension fails to ``dlopen`` (``Symbol not found:
_XML_SetAllocTrackerActivationThreshold``). The auth-token-path lookup
(``omnigent.cli_auth._token_file_path``) imports ``omnigent_ui_sdk.terminal``,
which pulls in prompt_toolkit; prompt_toolkit evaluates an ``HTML(...)`` template
at module-import time, which parses XML and needs ``pyexpat``. A broken
``pyexpat`` therefore turns a plain state-directory lookup into a hard crash,
long before the CLI reaches its connect step.

We reproduce the broken environment by making ``pyexpat`` unimportable for the
spawned CLI (a ``sitecustomize`` meta-path finder that raises the ticket's
dlopen error), then drive the real ``omnigent claude`` command and assert it
degrades to its normal connect flow instead of crashing on the import chain.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.e2e._native_resume_helpers import omnigent_console_script

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The worktree source dirs the CLI resolves at runtime. `omnigent_ui_sdk` (and
# its `omnigent_client` dependency) live under `sdks/`, so they must be on the
# child's PYTHONPATH for the ticket's real crash chain (auth-path lookup ->
# omnigent_ui_sdk -> prompt_toolkit -> pyexpat) to be reached rather than a
# spurious ModuleNotFoundError.
_PYTHONPATH_DIRS = (_REPO_ROOT, _REPO_ROOT / "sdks" / "ui", _REPO_ROOT / "sdks" / "python-client")

_DLOPEN_SYMBOL = "_XML_SetAllocTrackerActivationThreshold"
_DLOPEN_ERROR = (
    "dlopen(/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/lib/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so, 0x0002): "
    f"Symbol not found: {_DLOPEN_SYMBOL}"
)

# Loaded automatically at interpreter startup when its directory is first on
# PYTHONPATH; it makes ``import pyexpat`` fail the same way on any platform.
_SITECUSTOMIZE = f"""\
import sys


class _BrokenPyexpatFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pyexpat":
            raise ImportError({_DLOPEN_ERROR!r})
        return None


sys.meta_path.insert(0, _BrokenPyexpatFinder())
"""

# Almost certainly closed, so the connect step fails fast with its actionable
# "Could not reach the omnigent server" error rather than hanging.
_UNREACHABLE_SERVER = "http://127.0.0.1:59999"


def _run_claude_with_broken_pyexpat(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    shim_dir = tmp_path / "pyexpat-shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    env = dict(os.environ)
    for stale in ("OMNIGENT_REMOTE_AUTH_TOKEN", "DATABRICKS_TOKEN", "ANTHROPIC_API_KEY"):
        env.pop(stale, None)
    pythonpath_parts = [str(shim_dir), *(str(p) for p in _PYTHONPATH_DIRS)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"

    return subprocess.run(
        [str(omnigent_console_script()), "claude", "--server", _UNREACHABLE_SERVER],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_claude_startup_survives_broken_pyexpat(tmp_path: Path) -> None:
    result = _run_claude_with_broken_pyexpat(tmp_path)
    combined = result.stdout + result.stderr

    assert _DLOPEN_SYMBOL not in combined, (
        "`omnigent claude` crashed on the pyexpat import chain "
        "(auth-token-path lookup dragged in prompt_toolkit) instead of "
        f"reaching its connect step.\n\n{combined}"
    )
    assert "Could not reach the omnigent server" in combined, (
        "`omnigent claude` did not reach its connect step under a broken "
        f"pyexpat.\n\nexit={result.returncode}\n{combined}"
    )
