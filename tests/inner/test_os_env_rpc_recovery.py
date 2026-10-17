"""A lost helper response must not apply the same mutation twice."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.environment_filesystem import InvalidPath, ResourceError
from omnigent.inner import os_env
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.environment_filesystem import CallerProcessFilesystem

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _inactive_policy() -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


class _ReplyProcess:
    """Apply real helper operations, then fail one transport boundary."""

    def __init__(
        self, receive: Callable[[dict[str, Any]], dict[str, Any]], fault: str | None
    ) -> None:
        self.receive = receive
        self.fault = fault
        self.response: dict[str, Any] = {}
        self.returncode: int | None = None
        self.stdin = _RequestInput(self)
        self.stdout = _ReplyOutput(self)
        self.stderr = io.StringIO()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class _RequestInput(io.StringIO):
    def __init__(self, process: _ReplyProcess) -> None:
        super().__init__()
        self.process = process

    def write(self, payload: str) -> int:
        self.process.response = self.process.receive(json.loads(payload))
        return len(payload)

    def flush(self) -> None:
        if self.process.fault == "flush":
            raise BrokenPipeError("reply lost after the request was applied")


class _ReplyOutput(io.StringIO):
    def __init__(self, process: _ReplyProcess) -> None:
        super().__init__()
        self.process = process

    def readline(self, size: int = -1) -> str:
        if self.process.fault == "eof":
            self.process.returncode = 1
            return ""
        if self.process.fault == "malformed":
            return "{incomplete reply\n"
        return json.dumps(self.process.response) + "\n"


def _client(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    receive: Callable[[dict[str, Any]], dict[str, Any]],
    fault: str | None,
    *,
    every_start: bool = False,
) -> tuple[os_env._HelperProcessClient, list[_ReplyProcess]]:
    client = os_env._HelperProcessClient(
        cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy()
    )
    starts: list[_ReplyProcess] = []

    def start() -> None:
        process = _ReplyProcess(receive, fault if not starts or every_start else None)
        starts.append(process)
        client._proc = process  # type: ignore[assignment]

    monkeypatch.setattr(client, "_start_unchecked_locked", start)
    return client, starts


@pytest.mark.parametrize("fault", ["flush", "eof", "malformed"])
@pytest.mark.parametrize("operation", ["write", "edit", "delete", "shell", "future-op"])
def test_unconfirmed_mutation_is_not_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("needle")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep me")
    (tmp_path / "link").symlink_to(outside)
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("needle from replacement B")
    requests: list[dict[str, Any]] = []
    policy = _inactive_policy()
    payloads: dict[str, dict[str, Any]] = {
        "write": {"op": "write", "path": "file.txt", "content": "applied"},
        "edit": {"op": "edit", "path": "file.txt", "oldText": "needle", "newText": "applied"},
        "delete": {"op": "delete", "path": "link"},
        "shell": {"op": "shell", "command": "printf applied >> counter.txt"},
        "future-op": {"op": "future-op"},
    }

    def receive(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        result = os_env._handle_helper_request(
            request=payload, cwd=tmp_path, shell_path="/bin/sh", sandbox=policy
        )
        if len(requests) == 1 and operation in {"write", "edit"}:
            os.replace(replacement, path)
        return result

    client, starts = _client(monkeypatch, tmp_path, receive, fault)
    try:
        result = client.request(payloads[operation])
    finally:
        client.close()

    assert len(requests) == 1
    assert len(starts) == 1
    assert result["code"] == "operation_unconfirmed"
    assert "may have completed" in result["error"]
    cause_type, cause_detail = {
        "flush": ("BrokenPipeError", "reply lost after the request was applied"),
        "eof": ("RuntimeError", "OS environment helper exited with code 1"),
        "malformed": ("JSONDecodeError", "Expecting property name"),
    }[fault]
    assert result["cause_type"] == cause_type
    assert cause_detail in result["error"]
    if operation in {"write", "edit"}:
        assert path.read_text() == "needle from replacement B"
    elif operation == "delete":
        assert not os.path.lexists(tmp_path / "link")
        assert (outside / "victim.txt").read_text() == "keep me"
    elif operation == "shell":
        assert (tmp_path / "counter.txt").read_text() == "applied"


@pytest.mark.parametrize("fault", ["flush", "eof", "malformed"])
@pytest.mark.parametrize("every_start", [False, True])
def test_read_keeps_its_one_reconnect_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, every_start: bool
) -> None:
    (tmp_path / "file.txt").write_text("content")
    requests: list[dict[str, Any]] = []

    def receive(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return os_env._handle_helper_request(
            request=payload, cwd=tmp_path, shell_path="/bin/sh", sandbox=_inactive_policy()
        )

    client, starts = _client(monkeypatch, tmp_path, receive, fault, every_start=every_start)
    try:
        result = client.request({"op": "read", "path": "file.txt"})
    finally:
        client.close()

    assert len(requests) == 2
    assert len(starts) == 2
    if every_start:
        assert "error" in result
    else:
        assert result["content"] == "content"


def test_a_later_independent_request_reconnects_after_an_unconfirmed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    def receive(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return os_env._handle_helper_request(
            request=payload, cwd=tmp_path, shell_path="/bin/sh", sandbox=_inactive_policy()
        )

    client, starts = _client(monkeypatch, tmp_path, receive, "eof")
    try:
        first = client.request({"op": "write", "path": "first.txt", "content": "first"})
        second = client.request({"op": "write", "path": "second.txt", "content": "second"})
    finally:
        client.close()

    assert first["code"] == "operation_unconfirmed"
    assert second["bytes_written"] == len("second")
    assert [payload["path"] for payload in requests] == ["first.txt", "second.txt"]
    assert len(starts) == 2


def test_a_dead_helper_restarts_before_a_new_mutation_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    def receive(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return os_env._handle_helper_request(
            request=payload, cwd=tmp_path, shell_path="/bin/sh", sandbox=_inactive_policy()
        )

    client, starts = _client(monkeypatch, tmp_path, receive, None)
    dead = _ReplyProcess(receive, None)
    dead.returncode = 1
    client._proc = dead  # type: ignore[assignment]
    try:
        result = client.request({"op": "write", "path": "file.txt", "content": "once"})
    finally:
        client.close()

    assert result["bytes_written"] == 4
    assert len(requests) == 1
    assert len(starts) == 1


def test_closed_client_cannot_restart_for_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, starts = _client(monkeypatch, tmp_path, lambda _request: {}, None)
    client.close()

    assert client.request({"op": "read", "path": "file.txt"}) == {
        "error": "OS environment helper is closed"
    }
    assert starts == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("bwrap") is None or not sys.platform.startswith("linux"),
    reason="requires Linux and bwrap",
)
@pytest.mark.parametrize("replace", [False, True])
async def test_lost_real_delete_reply_preserves_the_current_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    """A reply lost after actual unlink cannot authorize deleting replacement B."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep A")
    outside_b = tmp_path / "outside-b"
    outside_b.mkdir()
    (outside_b / "victim.txt").write_text("keep B")
    link = workspace / "link"
    link.symlink_to(outside)
    monkeypatch.setenv(
        "PYTHONPATH", str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    environment = os_env.create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap", read_paths=[str(_REPO_ROOT)], write_paths=[str(workspace)]
            ),
        )
    )
    assert environment is not None
    fs = CallerProcessFilesystem(environment)
    client = environment._helper
    start = client._start_unchecked_locked
    dropped: list[dict[str, Any]] = []

    class LostReply:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def readline(self) -> str:
            line = self.stream.readline()
            result = json.loads(line)
            if result.get("deleted") is True and result.get("type") == "symlink" and not dropped:
                assert not os.path.lexists(link)
                dropped.append(result)
                if replace:
                    link.symlink_to(outside_b)
                raise OSError("one reply lost after actual private unlink")
            return line

    def observed_start() -> None:
        start()
        assert client._proc is not None and client._proc.stdout is not None
        client._proc.stdout = LostReply(client._proc.stdout)

    monkeypatch.setattr(client, "_start_unchecked_locked", observed_start)
    try:
        with pytest.raises(InvalidPath):
            await fs.delete("link/victim.txt")
        with pytest.raises(ResourceError, match="may have completed"):
            await fs.delete("link")
    finally:
        environment.close()

    assert len(dropped) == 1
    assert link.is_symlink() is replace
    if replace:
        assert link.readlink() == outside_b
    assert (outside / "victim.txt").read_text() == "keep A"
    assert (outside_b / "victim.txt").read_text() == "keep B"
