"""Local OpenSSH Git server used by broker and sandbox tests."""

from __future__ import annotations

import getpass
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import GitSshBinding


def _run(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=True)


@pytest.fixture
def ssh_git_server(tmp_path: Path) -> Iterator[tuple[GitSshBinding, str, Path]]:
    sshd = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else None)
    if sshd is None:
        pytest.skip("OpenSSH server is unavailable")
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    for path in (host_key, client_key):
        _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path))
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text((tmp_path / "client_key.pub").read_text())
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"127.0.0.1 {(tmp_path / 'host_key.pub').read_text()}")
    server_config = tmp_path / "sshd_config"
    server_config.write_text(
        f"Port {port}\n"
        "ListenAddress 0.0.0.0\n"
        f"HostKey {host_key}\n"
        f"AuthorizedKeysFile {authorized_keys}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\n"
        "StrictModes no\nPasswordAuthentication no\nPubkeyAuthentication yes\nUsePAM no\n"
        "LogLevel ERROR\n"
    )
    _run(sshd, "-t", "-f", str(server_config))
    server_log = (tmp_path / "sshd.log").open("wb")
    server = subprocess.Popen([sshd, "-D", "-e", "-f", str(server_config)], stderr=server_log)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"sshd failed to listen: {(tmp_path / 'sshd.log').read_text()}")
        repo = tmp_path / "repo.git"
        work = tmp_path / "work"
        _run("git", "init", "--bare", str(repo))
        _run("git", "init", str(work))
        (work / "README.md").write_text("Git SSH fixture\n")
        _run("git", "add", "README.md", cwd=work)
        _run(
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "initial",
            cwd=work,
        )
        _run("git", "remote", "add", "origin", str(repo), cwd=work)
        _run("git", "push", "origin", "HEAD:main", cwd=work)
        binding = GitSshBinding(
            host="127.0.0.1",
            port=port,
            username=getpass.getuser(),
            repository=str(repo),
            operations=frozenset({"fetch"}),
            identity_file=str(client_key),
            known_hosts_file=str(known_hosts),
            allowed_cidrs=("127.0.0.1/32",),
            allow_loopback=True,
        )
        url = f"ssh://{binding.username}@127.0.0.1:{port}{repo}"
        yield binding, url, work
    finally:
        server.terminate()
        server.wait(timeout=5)
        server_log.close()
