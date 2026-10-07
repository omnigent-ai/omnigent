"""Real Git and OpenSSH checks for the repository-scoped parent broker."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.harnesses.claude_native.bridge import _bridge_sandbox_payload
from omnigent.inner.datamodel import GitSshBinding, OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.git_ssh import (
    GitSshBroker,
    GitSshDenied,
    _parse_git_argv,
    _read_frame,
    _resolve_pinned_ip,
    _validate_pinned_host_key,
    _write_frame,
    apply_git_ssh_env,
    start_git_ssh_broker,
)
from omnigent.inner.loader import _parse_os_env_sandbox_spec
from omnigent.inner.os_env_serialization import decode_sandbox_spec, encode_sandbox_spec
from omnigent.inner.sandbox import resolve_sandbox
from omnigent.inner.terminal import build_terminal_os_env_spec
from omnigent.spec.parser import _parse_git_ssh, _parse_os_env_sandbox
from tests.inner.git_ssh_fixture import _run


def _git_env(broker: GitSshBroker) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    apply_git_ssh_env(env, broker)
    return env


@pytest.fixture
def broker_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="gitssh-", dir="/tmp") as folder:
        yield Path(folder)


def test_git_ssh_broker_fetch_and_denials(
    ssh_git_server: tuple[GitSshBinding, str, Path], tmp_path: Path, broker_dir: Path
) -> None:
    binding, url, work = ssh_git_server
    broker = start_git_ssh_broker([binding], broker_dir)
    env = _git_env(broker)
    try:
        cloned = tmp_path / "cloned"
        result = subprocess.run(
            ["git", "clone", "--branch", "main", url, str(cloned)],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (cloned / "README.md").read_text() == "Git SSH fixture\n"

        _run("git", "-C", str(work), "checkout", "-b", "second")
        (work / "second.txt").write_text("second\n")
        _run("git", "-C", str(work), "add", "second.txt")
        _run(
            "git",
            "-C",
            str(work),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "second",
        )
        _run("git", "-C", str(work), "push", "origin", "HEAD:second")
        fetched = subprocess.run(
            ["git", "-C", str(cloned), "fetch", "origin", "second"],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert fetched.returncode == 0, fetched.stderr

        denied = subprocess.run(
            ["git", "ls-remote", url.replace("repo.git", "other.git")],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert denied.returncode != 0
        assert "not allowed" in denied.stderr

        push = subprocess.run(
            ["git", "push", url, "HEAD:other"],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert push.returncode != 0
        assert "not allowed" in push.stderr
    finally:
        broker.stop()


def test_git_ssh_broker_push_requires_write_binding(
    ssh_git_server: tuple[GitSshBinding, str, Path], broker_dir: Path
) -> None:
    binding, url, work = ssh_git_server
    broker = start_git_ssh_broker([replace(binding, operations=frozenset({"push"}))], broker_dir)
    try:
        result = subprocess.run(
            ["git", "push", url, "HEAD:refs/heads/write-grant"],
            cwd=work,
            env=_git_env(broker),
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert _run(
            "git", "--git-dir", binding.repository, "rev-parse", "refs/heads/write-grant"
        ).stdout.strip()
        denied = subprocess.run(
            ["git", "ls-remote", url],
            env=_git_env(broker),
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert denied.returncode != 0
        assert "not allowed" in denied.stderr
    finally:
        broker.stop()


def test_git_ssh_broker_uses_dns_name_and_pinned_address(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, _ = ssh_git_server
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 80))
        except OSError:
            pytest.skip("no non-loopback local address is available")
        local_ip = probe.getsockname()[0]
    if local_ip.startswith("127."):
        pytest.skip("no non-loopback local address is available")
    known_hosts = tmp_path / "dns_known_hosts"
    public_key = Path(source.known_hosts_file).read_text().split(maxsplit=1)[1]
    known_hosts.write_text(f"git.example.test {public_key}")
    binding = replace(
        source,
        host="git.example.test",
        known_hosts_file=str(known_hosts),
        allowed_cidrs=(f"{local_ip}/32",),
        allow_loopback=False,
    )
    original_getaddrinfo = socket.getaddrinfo

    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        if host == "git.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (local_ip, port))]
        return original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    broker = start_git_ssh_broker([binding], broker_dir)
    try:
        url = f"ssh://{binding.username}@git.example.test:{binding.port}{binding.repository}"
        result = subprocess.run(
            ["git", "clone", "--branch", "main", url, str(tmp_path / "dns-clone")],
            env=_git_env(broker),
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "dns-clone" / "README.md").read_text() == "Git SSH fixture\n"
    finally:
        broker.stop()


@pytest.mark.parametrize(
    "command",
    [
        "sh -c id",
        "git-upload-pack '/repo.git; id'",
        "git-upload-pack '/other.git'",
        "git-upload-pack '/repo.git' && id",
    ],
)
def test_git_ssh_rejects_other_commands(command: str, tmp_path: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    with pytest.raises(GitSshDenied):
        _parse_git_argv(["git@git.example.test", command], [binding])


def test_git_ssh_rejects_mixed_dns_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 22)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 22)),
        ],
    )
    with pytest.raises(GitSshDenied, match="blocked address"):
        _resolve_pinned_ip(binding)


def test_git_ssh_parser_accepts_exact_dns_binding(tmp_path: Path) -> None:
    binding = {
        "host": "Git.Example.Test",
        "username": "git",
        "repository": "org/repo.git",
        "identity_file": str(tmp_path / "key"),
        "known_hosts_file": str(tmp_path / "known_hosts"),
    }
    parsed = _parse_git_ssh([binding])
    assert parsed is not None
    assert parsed[0].host == "git.example.test"
    assert parsed[0].port == 22
    assert parsed[0].operations == frozenset({"fetch"})

    raw = {"type": "linux_bwrap", "git_ssh": [binding]}
    for parser in (_parse_os_env_sandbox, _parse_os_env_sandbox_spec):
        sandbox = parser(raw)
        assert sandbox.git_ssh == parsed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "*.example.test"),
        ("host", "git.example.test;id"),
        ("port", 0),
        ("port", True),
        ("repository", "../other.git"),
        ("repository", "-help"),
        ("repository", "repo.git;id"),
        ("operations", ["shell"]),
        ("allowed_cidrs", ["0.0.0.0/0;id"]),
        ("allow_loopback", "yes"),
    ],
)
def test_git_ssh_parser_rejects_unsafe_binding(tmp_path: Path, field: str, value: object) -> None:
    binding: dict[str, object] = {
        "host": "git.example.test",
        "repository": "org/repo.git",
        "identity_file": str(tmp_path / "key"),
        "known_hosts_file": str(tmp_path / "known_hosts"),
    }
    binding[field] = value
    with pytest.raises(OmnigentError):
        _parse_git_ssh([binding])


def test_git_ssh_loopback_requires_exact_local_grant(tmp_path: Path) -> None:
    binding: dict[str, object] = {
        "host": "127.0.0.1",
        "repository": "org/repo.git",
        "identity_file": str(tmp_path / "key"),
        "known_hosts_file": str(tmp_path / "known_hosts"),
        "allow_loopback": True,
        "allowed_cidrs": ["127.0.0.1/32"],
    }
    assert _parse_git_ssh([binding]) is not None
    binding["allowed_cidrs"] = ["127.0.0.0/8"]
    with pytest.raises(OmnigentError, match="exact host CIDR"):
        _parse_git_ssh([binding])
    binding["host"] = "localhost"
    with pytest.raises(OmnigentError, match="loopback IP literal"):
        _parse_git_ssh([binding])


def test_git_ssh_programmatic_binding_cannot_disable_containment(tmp_path: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    with pytest.raises(ValueError, match="git_ssh requires"):
        resolve_sandbox(
            OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(type="none", git_ssh=[binding]),
            ),
            tmp_path,
        )
    with pytest.raises(OmnigentError, match="repository"):
        resolve_sandbox(
            OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(
                    type="darwin_seatbelt", git_ssh=[replace(binding, repository="repo.git;id")]
                ),
            ),
            tmp_path,
        )


def test_git_ssh_terminal_cannot_override_containment(tmp_path: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    parent = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap", git_ssh=[binding]))
    terminal = TerminalEnvSpec(command="bash", os_env="inherit", allow_sandbox_override=True)
    with pytest.raises(ValueError, match="git_ssh"):
        build_terminal_os_env_spec(terminal, parent_os_env_spec=parent, sandbox_override="none")


def test_git_ssh_binding_survives_native_bridge_json(tmp_path: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    sandbox = OSEnvSandboxSpec(type="linux_bwrap", git_ssh=[binding])
    for payload in (encode_sandbox_spec(sandbox), _bridge_sandbox_payload(sandbox)):
        restored = decode_sandbox_spec(json.loads(json.dumps(payload)))
        assert restored.git_ssh == [binding]


def test_git_ssh_rejects_non_exact_host_key_file(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "*.example.test ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    )
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(known_hosts),
    )
    with pytest.raises(GitSshDenied, match="exact pinned"):
        _validate_pinned_host_key(binding)


def test_git_ssh_revocation_terminates_active_connection(
    ssh_git_server: tuple[GitSshBinding, str, Path], broker_dir: Path
) -> None:
    binding, _, _ = ssh_git_server
    broker = start_git_ssh_broker([binding], broker_dir)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(broker.socket_path))
        request = {
            "token": broker.token,
            "argv": [
                "-p",
                str(binding.port),
                f"{binding.username}@{binding.host}",
                f"git-upload-pack '{binding.repository}'",
            ],
        }
        _write_frame(sock, b"R", json.dumps(request).encode(), threading.Lock())
        assert _read_frame(sock) == (b"A", b"")
        with broker._lock:
            processes = list(broker._active)
        assert len(processes) == 1
        assert processes[0].poll() is None
        broker.stop()
        processes[0].wait(timeout=5)
        assert processes[0].poll() is not None
        with pytest.raises(OSError):
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(str(broker.socket_path))
    finally:
        sock.close()
        broker.stop()


def test_git_ssh_socket_requires_its_own_capability(tmp_path: Path, broker_dir: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    first_dir = broker_dir / "a"
    second_dir = broker_dir / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = start_git_ssh_broker([binding], first_dir)
    second = start_git_ssh_broker([binding], second_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(second.socket_path))
            request = {
                "token": first.token,
                "argv": ["git@git.example.test", "git-upload-pack 'org/repo.git'"],
            }
            _write_frame(sock, b"R", json.dumps(request).encode(), threading.Lock())
            response = _read_frame(sock)
            assert response is not None and response[0] == b"D"
            assert b"capability is invalid" in response[1]
    finally:
        first.stop()
        second.stop()


def test_git_ssh_stop_closes_idle_handshake(tmp_path: Path, broker_dir: Path) -> None:
    binding = GitSshBinding(
        host="git.example.test",
        port=22,
        username="git",
        repository="org/repo.git",
        operations=frozenset({"fetch"}),
        identity_file=str(tmp_path / "key"),
        known_hosts_file=str(tmp_path / "known_hosts"),
    )
    broker = start_git_ssh_broker([binding], broker_dir)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(broker.socket_path))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with broker._lock:
                if broker._connections:
                    break
            time.sleep(0.01)
        broker.stop()
        sock.settimeout(1)
        assert sock.recv(1) == b""


def test_git_ssh_broker_rejects_wrong_host_key(
    ssh_git_server: tuple[GitSshBinding, str, Path], tmp_path: Path, broker_dir: Path
) -> None:
    binding, url, _ = ssh_git_server
    wrong = tmp_path / "wrong_known_hosts"
    wrong_key = tmp_path / "wrong_key"
    _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key))
    wrong.write_text(f"127.0.0.1 {(tmp_path / 'wrong_key.pub').read_text()}")
    broker = start_git_ssh_broker([replace(binding, known_hosts_file=str(wrong))], broker_dir)
    try:
        result = subprocess.run(
            ["git", "ls-remote", url],
            env=_git_env(broker),
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert (
            "Host key verification failed" in result.stderr
            or "REMOTE HOST IDENTIFICATION HAS CHANGED" in result.stderr
        )
    finally:
        broker.stop()
