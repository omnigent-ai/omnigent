"""Real tool processes must survive a broken checkout without hiding it from tests."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import (
    CallerProcessOSEnvironment,
    _project_root,
    create_os_environment,
)

pytestmark = pytest.mark.posix_only


@pytest.mark.parametrize(
    "backend",
    [
        "none",
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                sys.platform != "linux" or shutil.which("bwrap") is None,
                reason="requires Linux and bwrap",
            ),
        ),
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS"),
        ),
    ],
)
@pytest.mark.parametrize("scenario", ["startup", "restart", "fork"])
def test_tools_repair_broken_checkout_with_normal_shell_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, scenario: str
) -> None:
    """Repair conflict markers using real file tools, then test the repaired code.

    The restart case corrupts the checkout after the helper starts and kills
    that process. Its replacement must still import the stable runtime. Shell
    commands must see checkout changes and script siblings with normal Python
    imports, without inheriting the helper's safe-path setting.
    """
    workspace = tmp_path.resolve() / "checkout"
    package = workspace / "omnigent"
    package.mkdir(parents=True)
    broken_source = "<<<<<<< HEAD\n=======\n>>>>>>> fix\n"
    module = package / "__init__.py"
    if scenario != "restart":
        module.write_text(broken_source)
    scripts = workspace / "scripts"
    scripts.mkdir()
    (scripts / "support.py").write_text("VALUE = 'script sibling'\n")
    (scripts / "check.py").write_text(
        "import os, sys\n"
        "from support import VALUE\n"
        "assert not sys.flags.safe_path\n"
        "assert 'PYTHONSAFEPATH' not in os.environ\n"
        "print(VALUE)\n"
    )
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    # Even an explicit cwd entry must follow the stable runtime for the helper.
    monkeypatch.setenv("PYTHONPATH", ".")
    env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            fork=scenario == "fork",
            sandbox=OSEnvSandboxSpec(
                type=backend,
                read_paths=[str(_project_root() / "omnigent")],
                write_paths=["."],
            ),
        )
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    python = shlex.quote(sys.executable)

    async def exercise() -> None:
        if scenario == "restart":
            ready = await env.shell("echo ready")
            assert ready.get("stdout", "").strip() == "ready", ready
            module.write_text(broken_source)
            proc = env._helper._proc
            assert proc is not None
            proc.kill()
            proc.wait(timeout=10)

        source = await env.read("omnigent/__init__.py")
        assert source.get("content") == broken_source, source
        broken = await env.shell(f"{python} -c 'import omnigent'")
        assert broken.get("exit_code") != 0, broken
        assert "SyntaxError" in broken.get("stderr", ""), broken

        written = await env.write("omnigent/__init__.py", "VALUE = 'before'\n")
        assert "error" not in written, written
        edited = await env.edit("omnigent/__init__.py", old_text="before", new_text="repaired")
        assert "error" not in edited, edited
        repaired = await env.shell(f"{python} -c 'import omnigent; print(omnigent.VALUE)'")
        assert repaired.get("exit_code") == 0, repaired
        assert repaired.get("stdout", "").strip() == "repaired", repaired

        sibling = await env.shell(f"{python} scripts/check.py")
        assert sibling.get("exit_code") == 0, sibling
        assert sibling.get("stdout", "").strip() == "script sibling", sibling
        if scenario == "fork":
            assert module.read_text() == broken_source

    try:
        asyncio.run(exercise())
    finally:
        env.close()
