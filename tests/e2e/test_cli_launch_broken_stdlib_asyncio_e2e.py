"""Bare ``omnigent`` must diagnose an unimportable stdlib instead of crash-reporting it.

The spawned CLI sees a shadow copy of the stdlib ``asyncio`` package whose ``runners.py``
imports ``aiohttp`` while a ``sitecustomize`` finder keeps ``aiohttp`` unimportable, so the
test interpreter itself is untouched.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

from tests.e2e._native_resume_helpers import omnigent_console_script

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHONPATH_DIRS = (_REPO_ROOT, _REPO_ROOT / "sdks" / "ui", _REPO_ROOT / "sdks" / "python-client")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
_ISSUE_PROMPT = r"file a GitHub issue with this report\? \[Y/n\]"
_INTERPRETER_BLAMED = re.compile(r"(?i)python installation|python interpreter|standard library")
_SHIM_NAME = "broken-stdlib"

_SITECUSTOMIZE = """\
import sys


class _MissingAiohttp:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "aiohttp" or fullname.startswith("aiohttp."):
            raise ModuleNotFoundError("No module named 'aiohttp'", name="aiohttp")
        return None


sys.meta_path.insert(0, _MissingAiohttp())
"""


def _broken_stdlib_shim(tmp_path: Path) -> Path:
    shim = tmp_path / _SHIM_NAME
    stdlib_asyncio = Path(sysconfig.get_paths()["stdlib"]) / "asyncio"
    shutil.copytree(stdlib_asyncio, shim / "asyncio", ignore=shutil.ignore_patterns("__pycache__"))
    runners = shim / "asyncio" / "runners.py"
    lines = runners.read_text(encoding="utf-8").splitlines(keepends=True)
    # Before the module's first real import, wherever this Python version puts it.
    first_import = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
        ),
        None,
    )
    if first_import is None:
        pytest.fail(f"no top-level import line found in {runners}; the shim needs updating")
    lines.insert(first_import, "import aiohttp\n")
    runners.write_text("".join(lines), encoding="utf-8")
    (shim / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return shim


def _fresh_cli_env(tmp_path: Path, shim: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNIGENT_", "RUNNER_"))}
    for stale in (
        "DATABRICKS_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "TMUX",
        "VIRTUAL_ENV",
    ):
        env.pop(stale, None)
    pythonpath = [str(shim), *(str(p) for p in _PYTHONPATH_DIRS)]
    if os.environ.get("PYTHONPATH"):
        pythonpath.append(os.environ["PYTHONPATH"])
    for name in ("home", "config", "data"):
        (tmp_path / name).mkdir()
    env.update(
        PYTHONPATH=os.pathsep.join(pythonpath),
        HOME=str(tmp_path / "home"),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "config"),
        OMNIGENT_DATA_DIR=str(tmp_path / "data"),
        OMNIGENT_NO_UPDATE_CHECK="1",
        TERM="xterm-256color",
    )
    return env


def _run_bare_omnigent(env: dict[str, str]) -> tuple[int | str, str]:
    script = str(omnigent_console_script())
    child = pexpect.spawn(
        script,
        [],
        env=env,
        cwd=env["HOME"],
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=120,
    )
    output = ""
    try:
        while True:
            try:
                index = child.expect([_ISSUE_PROMPT, pexpect.EOF], timeout=120)
            except pexpect.TIMEOUT:
                output += child.before or ""  # keep what was seen so the cleanup check sees it too
                raise
            output += child.before or ""
            if index == 1:
                break
            output += child.after
            child.sendline("n")
    finally:
        child.close()
        if "Started the host daemon" in output:
            # The shim that broke asyncio for the launch must not also break the cleanup.
            pythonpath = env["PYTHONPATH"].split(os.pathsep)
            clean_env = dict(
                env,
                PYTHONPATH=os.pathsep.join(p for p in pythonpath if not p.endswith(_SHIM_NAME)),
            )
            # Cleanup is best-effort and must not mask the primary failure.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(
                    [script, "stop"], env=clean_env, capture_output=True, timeout=90, check=False
                )
    # A signal death reports no exit status; name the signal so the failure is diagnosable.
    status = child.exitstatus if child.exitstatus is not None else f"signal {child.signalstatus}"
    return status, _ANSI_RE.sub("", output)


def test_launch_blames_the_broken_python_stdlib_not_omnigent(tmp_path: Path) -> None:
    env = _fresh_cli_env(tmp_path, _broken_stdlib_shim(tmp_path))
    status, output = _run_bare_omnigent(env)

    assert status == 1, (
        f"omnigent did not exit with status 1 (got {status!r}) although the interpreter's "
        f"asyncio is broken:\n{output}"
    )
    assert "No module named 'aiohttp'" in output, (
        f"the emulated stdlib fault did not fire; cannot judge the launch output:\n{output}"
    )
    assert not re.search(_ISSUE_PROMPT, output), (
        "a broken Python installation was presented as an omnigent crash to report "
        f"(this is how the ticket was auto-filed):\n{output}"
    )
    assert not list((tmp_path / "data" / "crashes").glob("crash-*.md")), (
        "a crash report was written for a failure that is not an omnigent crash"
    )
    assert _INTERPRETER_BLAMED.search(output), (
        "the launch failure does not tell the user their Python installation / standard "
        f"library is broken; it only shows the bare import error:\n{output}"
    )
