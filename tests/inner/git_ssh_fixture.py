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


def _nonloopback_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 80))
        except OSError:
            return None
        address = probe.getsockname()[0]
    return address if not address.startswith("127.") else None


def _stop_sshd(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is None:
        server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def _has_host_key(address: str, port: int, expected_key: list[str]) -> bool:
    try:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", "1", "-p", str(port), "-t", "ed25519", address],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return any(line.split()[1:3] == expected_key for line in scan.stdout.splitlines())


@pytest.fixture
def ssh_git_server(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[GitSshBinding, str, Path]]:
    sshd = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else None)
    if sshd is None:
        pytest.skip("OpenSSH server is unavailable")
    listen_ip = None
    if getattr(request, "param", None) == "nonloopback":
        listen_ip = _nonloopback_ip()
        if listen_ip is None:
            pytest.skip("no non-loopback local address is available")
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    for path in (host_key, client_key):
        _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path))
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text((tmp_path / "client_key.pub").read_text())
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"127.0.0.1 {(tmp_path / 'host_key.pub').read_text()}")
    expected_key = (tmp_path / "host_key.pub").read_text().split()[:2]
    server_config = tmp_path / "sshd_config"
    listen_addresses = "ListenAddress 127.0.0.1\n"
    if listen_ip:
        listen_addresses += f"ListenAddress {listen_ip}\n"
    addresses = ["127.0.0.1"] + ([listen_ip] if listen_ip else [])
    with (tmp_path / "sshd.log").open("wb") as server_log:
        server: subprocess.Popen[bytes] | None = None
        for _ in range(3):
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            server_config.write_text(
                f"Port {port}\n"
                f"{listen_addresses}"
                f"HostKey {host_key}\n"
                f"AuthorizedKeysFile {authorized_keys}\n"
                f"PidFile {tmp_path / 'sshd.pid'}\n"
                "StrictModes no\nPasswordAuthentication no\nPubkeyAuthentication yes\nUsePAM no\n"
                "LogLevel ERROR\n"
            )
            _run(sshd, "-t", "-f", str(server_config))
            server = subprocess.Popen(
                [sshd, "-D", "-e", "-f", str(server_config)], stderr=server_log
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.poll() is None:
                if all(_has_host_key(address, port, expected_key) for address in addresses):
                    if server.poll() is None:
                        break
                time.sleep(0.05)
            else:
                _stop_sshd(server)
                server = None
                continue
            break
        if server is None:
            pytest.fail(f"sshd failed to listen: {(tmp_path / 'sshd.log').read_text()}")
        try:
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
            _stop_sshd(server)
