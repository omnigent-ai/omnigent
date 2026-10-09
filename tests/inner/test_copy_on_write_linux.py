"""Real Linux mounts shared by file helpers, runner tools, and tmux terminals."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec, WritePathSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool, _execute_terminal_tool
from omnigent.sandbox.copy_on_write import (
    SHARED_ENVIRONMENT_VAR,
    CopyOnWriteEnvironment,
    export_shared_environment,
)
from omnigent.terminals.registry import TerminalRegistry

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux OverlayFS required")


@pytest.fixture
def cow_spec(tmp_path: Path):
    bwrap = shutil.which("bwrap")
    if not bwrap or "--tmp-overlay" not in subprocess.check_output([bwrap, "--help"], text=True):
        if os.environ.get("OMNIGENT_TEST_REQUIRE_COW"):
            pytest.fail("Bubblewrap 0.11+ required")
        pytest.skip("Bubblewrap 0.11+ required")
    deps = tmp_path / "dependencies"
    deps.mkdir()
    (tmp_path / "artifacts").mkdir()
    (deps / "original").write_text("original")
    (deps / "deleted").write_text("keep me")
    (deps / "masked").write_text("hidden")
    (deps / "directory").mkdir()
    (deps / "directory/nested").write_text("nested original")
    (tmp_path / "readonly").write_text("read only")
    spec = OSEnvSpec(
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[str(Path(__file__).resolve().parents[2])],
            write_paths=["artifacts", WritePathSpec("dependencies", True)],
            mask_paths=["dependencies/masked"],
            allow_network=False,
        ),
    )
    probe = create_os_environment(spec)
    assert probe is not None
    try:
        probe.prepare_sandbox(probe.sandbox)
    except OSError as exc:
        if os.environ.get("OMNIGENT_TEST_REQUIRE_COW"):
            raise
        pytest.skip(str(exc))
    finally:
        probe.close()
    return spec


async def assert_shell(env, command: str, output: str = "") -> None:
    result = await env.shell(command)
    assert result.get("exit_code") == 0, result
    assert str(result.get("stdout", "")).strip() == output, result


@pytest.mark.asyncio
async def test_file_operations_survive_helper_restart_without_host_changes(cow_spec, tmp_path):
    env = create_os_environment(cow_spec)
    assert env is not None
    original_mode = (tmp_path / "dependencies/original").stat().st_mode
    try:
        assert "error" not in await env.write("dependencies/new", "created")
        assert "error" not in await env.edit(
            "dependencies/original", old_text="original", new_text="edited"
        )
        await assert_shell(
            env,
            "rm dependencies/deleted && chmod 600 dependencies/original && "
            "rm -r dependencies/directory && mkdir dependencies/directory && "
            "mkdir dependencies/new-directory && mv dependencies/new dependencies/renamed && "
            "cat dependencies/original dependencies/renamed && echo saved > artifacts/output",
            "editedcreated",
        )
        await assert_shell(env, "stat -c %a dependencies/original", "600")
        env._helper._stop_locked()
        await assert_shell(env, "stat -c %a dependencies/original", "600")
        await assert_shell(
            env, "test -d dependencies/new-directory && test ! -e dependencies/directory/nested"
        )
        assert (await env.read("dependencies/original"))["content"] == "edited"
        await assert_shell(
            env,
            "test ! -e dependencies/deleted && test ! -e dependencies/new && "
            "cat dependencies/renamed",
            "created",
        )
        assert (tmp_path / "dependencies/original").read_text() == "original"
        assert (tmp_path / "dependencies/original").stat().st_mode == original_mode
        assert (tmp_path / "dependencies/deleted").read_text() == "keep me"
        assert not (tmp_path / "dependencies/new").exists()
        assert not (tmp_path / "dependencies/renamed").exists()
        assert not (tmp_path / "dependencies/new-directory").exists()
        assert (tmp_path / "dependencies/directory/nested").read_text() == "nested original"
        assert (tmp_path / "artifacts/output").read_text() == "saved\n"
    finally:
        env.close()
    fresh = create_os_environment(cow_spec)
    try:
        await assert_shell(fresh, "cat dependencies/original", "original")
        await assert_shell(fresh, "test ! -e dependencies/new && test -e dependencies/deleted")
    finally:
        fresh.close()


@pytest.mark.asyncio
async def test_independent_environments_and_borrowed_harness(cow_spec, monkeypatch):
    first = create_os_environment(cow_spec)
    second = create_os_environment(cow_spec)
    borrowed = None
    try:
        await assert_shell(first, "echo first > dependencies/original")
        await assert_shell(second, "cat dependencies/original", "original")
        monkeypatch.setenv(SHARED_ENVIRONMENT_VAR, export_shared_environment(first.sandbox))
        borrowed = create_os_environment(cow_spec)
        await assert_shell(borrowed, "cat dependencies/original", "first")
        await assert_shell(borrowed, "echo borrowed > dependencies/original")
        borrowed.close()
        await assert_shell(first, "cat dependencies/original", "borrowed")
    finally:
        if borrowed:
            borrowed.close()
        second.close()
        first.close()


@pytest.mark.asyncio
async def test_masks_readonly_paths_and_persistent_parent(cow_spec, tmp_path):
    env = create_os_environment(cow_spec)
    try:
        assert "error" in await env.write("readonly", "changed")
        await assert_shell(env, "test ! -s dependencies/masked && ! echo changed > readonly")
        assert "hidden" not in str(await env.read("dependencies/masked"))
    finally:
        env.close()
    spec = replace(
        cow_spec,
        sandbox=replace(cow_spec.sandbox, write_paths=[".", WritePathSpec("dependencies", True)]),
    )
    env = create_os_environment(spec)
    try:
        await assert_shell(env, "echo changed > dependencies/original && echo kept > persistent")
        assert (tmp_path / "dependencies/original").read_text() == "original"
        assert (tmp_path / "persistent").read_text() == "kept\n"
        await assert_shell(env, "cat dependencies/original", "changed")
    finally:
        env.close()


@pytest.mark.asyncio
async def test_runner_tools_share_terminal_and_reopened_terminal(cow_spec, tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    terminals = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminals, runner_workspace=tmp_path)
    terminal_spec = TerminalEnvSpec(
        command="bash", args=["--noprofile", "--norc"], os_env="inherit", allow_cwd_override=True
    )

    agent = SimpleNamespace(os_env=replace(cow_spec, cwd="."), terminals={"bash": terminal_spec})

    async def tool(name, **args):
        return await _execute_os_env_tool(
            name,
            args,
            agent_spec=agent,
            conversation_id="cow-session",
            runner_workspace=tmp_path,
            resource_registry=registry,
        )

    try:
        result = await tool("sys_os_write", path="dependencies/original", content="from tool")
        assert "error" not in result, result
        env = registry._resolve_primary("cow-session", agent)
        await assert_shell(env, "mkdir dependencies/terminal-cwd")
        for iteration in range(2):
            key = "shared"
            result = await _execute_terminal_tool(
                "sys_terminal_launch",
                {
                    "terminal": "bash",
                    "session": key,
                    "cwd": str(tmp_path / "dependencies/terminal-cwd"),
                },
                terminal_registry=terminals,
                resource_registry=registry,
                agent_spec=agent,
                conversation_id="cow-session",
                task_id="task",
                agent_id="agent",
                runner_workspace=tmp_path,
            )
            assert '"error"' not in result, result
            terminal = terminals.get("cow-session", "bash", key)
            assert terminal is not None
            await assert_shell(terminal.os_env, "pwd", str(tmp_path / "dependencies/terminal-cwd"))
            if iteration:
                await terminal.send("cat ../terminal > ../../artifacts/previous")
            await terminal.send(
                "cd ..; cat original > ../artifacts/seen; echo from-terminal > terminal; "
                "if test ! -s masked && ! echo forbidden > ../readonly; "
                "then echo yes > restricted; fi; echo ready > ../artifacts/ready"
            )
            env = registry._resolve_primary("cow-session", agent)
            for _ in range(100):
                if (tmp_path / "artifacts/ready").exists():
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail(str(await terminal.read()))
            assert (await env.read("dependencies/restricted")).get("content") == "yes\n", str(
                await terminal.read()
            )
            (tmp_path / "artifacts/ready").unlink()
            assert (tmp_path / "artifacts/seen").read_text() == "from tool"
            if iteration:
                assert (tmp_path / "artifacts/previous").read_text() == "from-terminal\n"
            result = await tool("sys_os_read", path="dependencies/terminal")
            assert "from-terminal" in result, result
            await terminals.close("cow-session", "bash", key)
            await assert_shell(env, "rm dependencies/restricted")
        assert not (tmp_path / "dependencies/terminal").exists()
        assert (tmp_path / "dependencies/original").read_text() == "original"
    finally:
        await registry.cleanup_session("cow-session")


@pytest.mark.asyncio
async def test_keeper_loss_does_not_run_against_host(cow_spec, tmp_path):
    env = create_os_environment(cow_spec)
    try:
        await assert_shell(env, "echo disposable > dependencies/original")
        keeper = env.copy_on_write_environment
        assert isinstance(keeper, CopyOnWriteEnvironment)
        keeper._process.kill()
        keeper._process.wait()
        env._helper._stop_locked()
        with pytest.raises(RuntimeError, match="lost"):
            await env.write("dependencies/original", "must not persist")
        assert (tmp_path / "dependencies/original").read_text() == "original"
    finally:
        env.close()


@pytest.mark.asyncio
async def test_session_cleanup_leaves_other_session_independent(cow_spec, tmp_path):
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    agent = SimpleNamespace(os_env=cow_spec)
    first = registry.resolve_environment("first", "default", agent)
    second = registry.resolve_environment("second", "default", agent)
    try:
        await assert_shell(first, "echo first > dependencies/original")
        await assert_shell(second, "cat dependencies/original", "original")
        await assert_shell(second, "echo second > dependencies/original")
        await registry.cleanup_session("first")
        await assert_shell(second, "cat dependencies/original", "second")
        fresh = registry.resolve_environment("first", "default", agent)
        await assert_shell(fresh, "cat dependencies/original", "original")
        assert (tmp_path / "dependencies/original").read_text() == "original"
    finally:
        await registry.cleanup_session("first")
        await registry.cleanup_session("second")
