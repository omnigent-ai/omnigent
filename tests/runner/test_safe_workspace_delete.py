"""Workspace delete confinement and confirmed-completion regressions."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities.environment_filesystem import ResourceError
from omnigent.inner import os_env as os_env_module
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, create_os_environment
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner import create_runner_app
from omnigent.runner.environment_filesystem import CallerProcessFilesystem
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.transports.ws_tunnel.serve import _send_hello
from omnigent.sandbox.copy_on_write import CopyOnWriteEnvironment
from tests.runner.helpers import NullServerClient

CAPABILITY = "workspace_delete_nofollow_v1"
FS_URL = "/v1/sessions/conv_test/resources/environments/default/filesystem"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "victim").write_bytes(b"workspace bytes")
    return root


@pytest.fixture
def environment(workspace: Path) -> Iterator[CallerProcessOSEnvironment]:
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    yield env
    env.close()


@pytest.fixture
async def client(
    environment: CallerProcessOSEnvironment, workspace: Path
) -> AsyncIterator[httpx.AsyncClient]:
    registry = SessionResourceRegistry()
    registry._primary_envs["conv_test"] = environment
    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as runner_client:
        yield runner_client


def _helper_delete(workspace: Path, path: str, *, recursive: bool = False) -> dict:
    return os_env_module._handle_helper_request(
        request={"op": "delete", "path": path, "recursive": recursive},
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=SandboxPolicy(
            backend_type="none",
            active=False,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )


@pytest.mark.asyncio
async def test_delete_uses_runner_helper_not_workspace_package(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    package = workspace / "omnigent"
    package.mkdir()
    (package / "__init__.py").write_text(
        "raise RuntimeError('workspace checkout must not supply the delete helper')\n"
    )
    response = await client.delete(f"{FS_URL}/victim")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert not (workspace / "victim").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("symlinked_ancestor", [False, True])
@pytest.mark.parametrize("helper_state", ["unstarted", "running", "restarted"])
async def test_delete_refuses_changed_workspace_ancestor(
    tmp_path: Path, symlinked_ancestor: bool, helper_state: str
) -> None:
    ancestor = tmp_path / "ancestor"
    root = ancestor / "workspace"
    root.mkdir(parents=True)
    configured_root = root
    if symlinked_ancestor:
        alias = tmp_path / "alias"
        alias.symlink_to(ancestor, target_is_directory=True)
        configured_root = alias / "workspace"
    outside = tmp_path / "outside"
    (outside / "workspace").mkdir(parents=True)
    outside_victim = outside / "workspace" / "victim"
    outside_victim.write_bytes(b"outside bytes")
    (root / "victim").write_bytes(b"workspace bytes")
    (root / "initial").write_bytes(b"initial alias works")
    env = create_os_environment(
        OSEnvSpec(cwd=str(configured_root), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    try:
        fs = CallerProcessFilesystem(env)
        if helper_state != "unstarted":
            assert (await fs.delete("initial")).deleted is True
        else:
            assert env._helper._proc is None
        moved = tmp_path / "moved"
        ancestor.rename(moved)
        ancestor.symlink_to(outside, target_is_directory=True)
        if helper_state == "restarted":
            with env._helper._lock:
                env._helper._stop_locked()
        with pytest.raises(ResourceError, match="Workspace root changed") as error:
            await fs.delete("victim")
        assert error.value.code == "workspace_root_changed"
        assert outside_victim.read_bytes() == b"outside bytes"
        assert (moved / "workspace" / "victim").read_bytes() == b"workspace bytes"
    finally:
        env.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_state", ["unstarted", "running", "restarted"])
async def test_workspace_root_changed_returns_conflict(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    helper_state: str,
) -> None:
    if helper_state != "unstarted":
        assert (await environment.shell("true"))["exit_code"] == 0
    previous = workspace.with_name("previous")
    workspace.rename(previous)
    workspace.mkdir()
    (workspace / "victim").write_bytes(b"replacement bytes")
    if helper_state == "restarted":
        with environment._helper._lock:
            environment._helper._stop_locked()
    response = await client.delete(f"{FS_URL}/victim")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workspace_root_changed"
    assert "Workspace root changed" in response.json()["error"]["message"]
    assert (workspace / "victim").read_bytes() == b"replacement bytes"
    assert (previous / "victim").read_bytes() == b"workspace bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sandbox_type",
    [
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                reason="darwin_seatbelt requires macOS + sandbox-exec",
            ),
        ),
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux") or not shutil.which("bwrap"),
                reason="linux_bwrap requires Linux + bwrap",
            ),
        ),
    ],
)
async def test_scratch_delete_under_active_sandbox(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_type: str
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    alias = tmp_path / "temporary-alias"
    alias.symlink_to(temporary, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    env = create_os_environment(
        OSEnvSpec(
            cwd=str(workspace),
            start_in_scratch=True,
            sandbox=OSEnvSandboxSpec(
                type=sandbox_type, read_paths=[str(Path(__file__).resolve().parents[2])]
            ),
        )
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    try:
        result = await env.shell("printf scratch > victim")
        assert result["exit_code"] == 0, result
        scratch = Path(result["cwd"])
        assert scratch != workspace
        assert (scratch / "victim").read_bytes() == b"scratch"
        assert (await CallerProcessFilesystem(env).delete("victim")).deleted is True
        assert not (scratch / "victim").exists()
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
    finally:
        env.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sandbox_type",
    [
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                reason="darwin_seatbelt requires macOS + sandbox-exec",
            ),
        ),
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux") or not shutil.which("bwrap"),
                reason="linux_bwrap requires Linux + bwrap",
            ),
        ),
    ],
)
async def test_fork_delete_under_active_sandbox(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_type: str
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    alias = tmp_path / "temporary-alias"
    alias.symlink_to(temporary, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    env = create_os_environment(
        OSEnvSpec(
            cwd=str(workspace),
            fork=True,
            sandbox=OSEnvSandboxSpec(
                type=sandbox_type,
                read_paths=[str(Path(__file__).resolve().parents[2])],
                write_paths=["."],
            ),
        )
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    fork_root = env.cwd
    try:
        assert (await env.shell("true"))["exit_code"] == 0
        assert (fork_root / "victim").read_bytes() == b"workspace bytes"
        assert (await CallerProcessFilesystem(env).delete("victim")).deleted is True
        assert not (fork_root / "victim").exists()
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert fork_root == fork_root.resolve()
    finally:
        env.close()
    assert not fork_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_state", ["unstarted", "restarted"])
@pytest.mark.parametrize(
    "sandbox_type",
    [
        "none",
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                reason="darwin_seatbelt requires macOS + sandbox-exec",
            ),
        ),
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux") or not shutil.which("bwrap"),
                reason="linux_bwrap requires Linux + bwrap",
            ),
        ),
    ],
)
async def test_missing_cwd_creation_preserves_operations_but_refuses_delete(
    tmp_path: Path, helper_state: str, sandbox_type: str
) -> None:
    cwd = tmp_path / "not-created-yet"
    spec = OSEnvSpec(
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(
            type=sandbox_type,
            read_paths=[str(Path(__file__).resolve().parents[2])],
            write_paths=["."],
        ),
    )
    env = create_os_environment(spec)
    assert isinstance(env, CallerProcessOSEnvironment)
    try:
        cwd.mkdir()
        assert (await env.shell("printf retained > victim"))["exit_code"] == 0
        assert (await env.read("victim"))["content"] == "retained"
        if helper_state == "restarted":
            with env._helper._lock:
                env._helper._stop_locked()
        result = await env.delete("victim")
        assert result["code"] == "workspace_root_changed"
        assert "create a new environment" in result["error"]
        assert (cwd / "victim").read_bytes() == b"retained"
    finally:
        env.close()
    replacement = create_os_environment(spec)
    assert isinstance(replacement, CallerProcessOSEnvironment)
    try:
        assert (await replacement.delete("victim"))["deleted"] is True
        assert not (cwd / "victim").exists()
    finally:
        replacement.close()


@pytest.mark.parametrize("failure", [PermissionError, RuntimeError])
@pytest.mark.parametrize("owned_copy_on_write", [False, True])
def test_failed_environment_initialization_cleans_owned_resources(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
    owned_copy_on_write: bool,
) -> None:
    fork_dir = tmp_path / "fork"
    fork_dir.mkdir()
    policy = SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    copy_on_write = Mock(spec=CopyOnWriteEnvironment)

    def fail_helper(*args: object, **kwargs: object) -> None:
        raise failure("initialization failed")

    monkeypatch.setattr(os_env_module, "_HelperProcessClient", fail_helper)
    env = CallerProcessOSEnvironment.__new__(CallerProcessOSEnvironment)
    with pytest.raises(failure, match="initialization failed"):
        env.__init__(
            spec=OSEnvSpec(cwd=str(workspace)),
            cwd=workspace,
            sandbox=policy,
            shell_path="/bin/sh",
            _fork_dir=fork_dir,
            _copy_on_write_environment=cast(CopyOnWriteEnvironment, copy_on_write),
            _owns_copy_on_write=owned_copy_on_write,
        )
    assert not fork_dir.exists()
    if owned_copy_on_write:
        copy_on_write.close.assert_called_once()
    else:
        copy_on_write.close.assert_not_called()
    env.close()
    env.__del__()


@pytest.mark.parametrize("stage", ["construction", "preparation"])
def test_failed_owned_namespace_initialization_cleans_fork(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    fork_dir = tmp_path / "fork"
    fork_dir.mkdir()
    policy = SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
        copy_on_write_roots=[workspace],
    )
    copy_on_write = Mock(spec=CopyOnWriteEnvironment)
    copy_on_write.prepare.side_effect = RuntimeError("namespace failed")
    factory = Mock(return_value=copy_on_write)
    if stage == "construction":
        factory.side_effect = RuntimeError("namespace failed")
    monkeypatch.setattr(os_env_module, "CopyOnWriteEnvironment", factory)
    env = CallerProcessOSEnvironment.__new__(CallerProcessOSEnvironment)
    with pytest.raises(RuntimeError, match="namespace failed"):
        env.__init__(
            spec=OSEnvSpec(cwd=str(workspace)),
            cwd=workspace,
            sandbox=policy,
            shell_path="/bin/sh",
            _fork_dir=fork_dir,
        )
    assert not fork_dir.exists()
    if stage == "preparation":
        copy_on_write.close.assert_called_once()
    else:
        copy_on_write.close.assert_not_called()
    env.close()
    env.__del__()


def test_root_identity_uses_prepared_environment_view(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = tmp_path / "namespace-view"
    view.mkdir()
    view_stat = view.stat()
    native_stat = Path.stat
    namespace_path = Path("/proc/123/root") / workspace.relative_to(workspace.anchor)

    def stat_root(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == namespace_path:
            return view_stat
        return native_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_root)
    policy = SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
        copy_on_write_roots=[workspace],
    )
    copy_on_write = Mock(spec=CopyOnWriteEnvironment)

    def prepare(sandbox: SandboxPolicy) -> None:
        sandbox.copy_on_write_namespace = (123, 456, 789)

    copy_on_write.prepare.side_effect = prepare
    helper = os_env_module._HelperProcessClient(
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=policy,
        copy_on_write_environment=cast(CopyOnWriteEnvironment, copy_on_write),
    )
    try:
        copy_on_write.prepare.assert_called_once_with(policy)
        assert helper._root_identity == (view_stat.st_dev, view_stat.st_ino)
    finally:
        helper.close()


@pytest.mark.asyncio
async def test_delete_records_symlinks_but_not_directories(
    client: httpx.AsyncClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = cast(httpx.ASGITransport, client._transport)
    registry = transport.app.state.filesystem_registry
    changes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(registry, "record_change", lambda *change: changes.append(change))
    (workspace / "link").symlink_to(workspace / "victim")
    (workspace / "directory").mkdir()
    assert (await client.delete(f"{FS_URL}/link")).status_code == 200
    assert (await client.delete(f"{FS_URL}/directory")).status_code == 200
    assert changes == [("link", "deleted", "conv_test")]
    assert (workspace / "victim").read_bytes() == b"workspace bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fifo", "socket"])
async def test_delete_classifies_special_entries_as_other(
    client: httpx.AsyncClient, workspace: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = workspace / "special"
    with socket.socket(socket.AF_UNIX) as unix_socket:
        if kind == "fifo":
            os.mkfifo(target)
        else:
            monkeypatch.chdir(workspace)
            unix_socket.bind(target.name)
        response = await client.delete(f"{FS_URL}/special")
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "other"
    assert response.json()["bytes_deleted"] is None
    assert not target.exists()


@pytest.mark.asyncio
async def test_symlinked_parent_preserves_outside_bytes(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"outside bytes")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    response = await client.delete(f"{FS_URL}/link/victim")
    assert victim.read_bytes() == b"outside bytes"
    assert (workspace / "victim").read_bytes() == b"workspace bytes"
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_leaf_symlinks_only_unlink_the_link(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    targets = [tmp_path / "file", tmp_path / "broken", tmp_path / "directory"]
    targets[0].write_bytes(b"outside file")
    targets[2].mkdir()
    (targets[2] / "child").write_bytes(b"outside child")
    for index, target in enumerate(targets):
        link = workspace / f"link{index}"
        link.symlink_to(target, target_is_directory=index == 2)
        response = await client.delete(f"{FS_URL}/{link.name}")
        assert response.status_code == 200
        assert not link.is_symlink()
        assert response.json()["type"] == "symlink"
        assert targets[0].read_bytes() == b"outside file"
        assert not targets[1].exists()
        assert (targets[2] / "child").read_bytes() == b"outside child"


@pytest.mark.asyncio
async def test_file_and_directory_delete_never_shells(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_shell(*args: object, **kwargs: object) -> dict:
        pytest.fail("delete must not shell out")

    def forbidden_local(*args: object, **kwargs: object) -> dict:
        pytest.fail("delete must run in the helper process")

    monkeypatch.setattr(environment, "shell", forbidden_shell)
    monkeypatch.setattr(os_env_module, "_delete_impl", forbidden_local, raising=False)
    directory = workspace / "directory"
    directory.mkdir()
    (directory / "child").write_bytes(b"child")
    response = await client.delete(f"{FS_URL}/directory")
    assert response.status_code == 409
    assert (directory / "child").read_bytes() == b"child"
    (directory / "child").unlink()
    assert (await client.delete(f"{FS_URL}/directory")).status_code == 200
    assert not directory.exists()
    assert (await client.delete(f"{FS_URL}/victim")).status_code == 200
    assert not (workspace / "victim").exists()
    assert (await client.delete(f"{FS_URL}/missing")).status_code == 404


@pytest.mark.asyncio
async def test_unreadable_directory_does_not_fail_open(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = workspace / "unreadable"
    directory.mkdir()
    child = directory / "child"
    child.write_bytes(b"keep")
    directory.chmod(0)
    original_shell = environment.shell

    async def restore_after_listing(command: str, **kwargs: object) -> dict:
        result = await original_shell(command, **kwargs)
        if "os.listdir" in command:
            assert result.get("exit_code") != 0
            directory.chmod(0o700)
        return result

    monkeypatch.setattr(environment, "shell", restore_after_listing)
    try:
        response = await client.delete(f"{FS_URL}/unreadable")
    finally:
        if directory.exists():
            directory.chmod(0o700)
    assert child.read_bytes() == b"keep"
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_unconfirmed_helper_results_never_succeed(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "absolute-victim"
    outside.write_bytes(b"outside")
    results = [
        {"error": "helper disconnected"},
        {},
        {"deleted": True},
        {"deleted": True, "exit_code": 1, "type": "file", "bytes_deleted": 1},
        {"deleted": True, "exit_code": 0, "type": "file", "error": "timeout"},
    ]
    for result in results:

        async def unconfirmed(
            *args: object, helper_result: dict = result, **kwargs: object
        ) -> dict:
            return helper_result

        monkeypatch.setattr(environment, "delete", unconfirmed, raising=False)
        response = await client.delete(f"{FS_URL}/victim")
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert response.status_code >= 400
        response = await client.delete(f"{FS_URL}/{outside}")
        assert outside.read_bytes() == b"outside"
        assert response.status_code >= 400
    for exception in (TimeoutError("timeout"), ConnectionError("disconnected")):

        async def interrupted(
            *args: object, failure: Exception = exception, **kwargs: object
        ) -> dict:
            raise failure

        monkeypatch.setattr(environment, "delete", interrupted)
        response = await client.delete(f"{FS_URL}/victim")
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert response.status_code >= 400


def test_helper_refuses_root_aliases(workspace: Path) -> None:
    for path in ("", ".", "./"):
        result = _helper_delete(workspace, path)
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert result.get("code") == "invalid_path"


def test_recursive_helper_never_follows_inner_symlink(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_bytes(b"outside")
    tree = workspace / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)
    result = _helper_delete(workspace, "tree", recursive=True)
    assert (outside / "child").read_bytes() == b"outside"
    assert result.get("deleted") is True or result.get("code") == "unsupported"
    assert not tree.exists() if result.get("deleted") else tree.is_dir()
    if not tree.exists():
        tree.mkdir()
        (tree / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    result = _helper_delete(workspace, "tree", recursive=True)
    assert result.get("code") == "unsupported"
    assert tree.is_dir()
    assert (outside / "child").read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_runner_recursive_delete_is_helper_only(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_bytes(b"outside")
    tree = workspace / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)

    async def forbidden_shell(*args: object, **kwargs: object) -> dict:
        pytest.fail("recursive delete must not shell out")

    monkeypatch.setattr(environment, "shell", forbidden_shell)
    response = await client.delete(f"{FS_URL}/tree?recursive=true")
    assert (outside / "child").read_bytes() == b"outside"
    assert response.status_code == 200
    assert not tree.exists()


def test_parent_swap_between_descriptor_steps_is_confined(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = workspace / "parent"
    parent.mkdir()
    (parent / "inner").mkdir()
    (parent / "inner" / "victim").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inner").mkdir()
    (outside / "inner" / "victim").write_bytes(b"outside")
    original_open = os.open

    def swap_after_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "parent":
            parent.rename(workspace / "moved")
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os_env_module.os, "open", swap_after_open)
    result = _helper_delete(workspace, "parent/inner/victim")
    assert (outside / "inner" / "victim").read_bytes() == b"outside"
    assert result.get("deleted") is True
    assert not (workspace / "moved" / "inner" / "victim").exists()


@pytest.mark.asyncio
async def test_capability_and_metadata_follow_platform_support(
    client: httpx.AsyncClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames: list[str] = []

    async def capture(frame: str) -> None:
        frames.append(frame)

    await _send_hello(capture, "test")
    assert CAPABILITY in json.loads(frames[-1])["capabilities"]
    response = await client.get(FS_URL.removesuffix("/filesystem"))
    assert response.json()["metadata"]["workspace_delete"] == {"available": True}
    monkeypatch.setattr(os_env_module, "SAFE_WORKSPACE_DELETE_SUPPORTED", False, raising=False)
    await _send_hello(capture, "test")
    assert CAPABILITY not in json.loads(frames[-1])["capabilities"]
    response = await client.get(FS_URL.removesuffix("/filesystem"))
    availability = response.json()["metadata"]["workspace_delete"]
    assert availability["available"] is False
    assert availability["reason"]
    result = _helper_delete(workspace, "victim")
    assert result.get("code") == "unsupported"
    assert (workspace / "victim").read_bytes() == b"workspace bytes"


def test_delete_disconnect_is_not_retried(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    helper = env._helper
    starts: list[bool] = []

    class DisconnectedProcess:
        stdin = io.StringIO()
        stdout = io.StringIO()

    def start() -> None:
        starts.append(True)
        helper._proc = DisconnectedProcess()

    monkeypatch.setattr(helper, "_ensure_started_locked", start)
    monkeypatch.setattr(helper, "_stop_locked", lambda: None)
    monkeypatch.setattr(helper, "_helper_exit_detail_locked", lambda: "disconnected")
    try:
        result = helper.request({"op": "delete", "path": "victim"})
        assert result.get("error")
        assert len(starts) == 1
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
    finally:
        helper._proc = None
        env.close()
