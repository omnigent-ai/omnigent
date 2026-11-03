"""The desktop app connects a machine it reaches over SSH: sign-in, then host.

The "laptop" (the desktop app's SSH connect code, its ssh client and a stand-in
browser) runs in its own network namespace, so its localhost is not the remote
machine's, as on a real laptop and EC2 box. ssh reaches the remote sshd through
a Unix socket, which crosses namespaces. The remote side runs the real omnigent
and Databricks CLIs against a fake workspace in front of a real local server.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests._helpers.polling import poll_until
from tests.e2e._host_login_workspace import USER_EMAIL, HostLoginWorkspace

_SSHD = "/usr/sbin/sshd"
_DRIVER = Path(__file__).with_name("_desktop_ssh_connect_driver.cjs")
_PASSPHRASE = "laptop-key-passphrase"
_BUSY_LAPTOP_PORT = 8020


def _can_unshare_network() -> bool:
    if not shutil.which("unshare"):
        return False
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="network namespaces are Linux-only"),
    pytest.mark.skipif(
        not all(
            shutil.which(tool) for tool in ("databricks", "node", "ssh", "ssh-keygen", "openssl")
        )
        or not Path(_SSHD).exists(),
        reason="needs the Databricks CLI, node, OpenSSH client and server, and openssl",
    ),
    pytest.mark.skipif(
        not _can_unshare_network(), reason="unprivileged network namespaces are unavailable"
    ),
    pytest.mark.timeout(240),
]

# Loopback probes disable trust_env (and hence SSL_CERT_FILE); give the box's
# CLI the test CA as its default bundle, keeping TLS verified.
_OMNIGENT_ENTRY = (
    "import os, certifi\n"
    "certifi.where = lambda: os.environ['SSL_CERT_FILE']\n"
    "from omnigent.cli import main\n"
    "main()\n"
)

_RELAY = """\
import os, socket, sys, threading
conn = socket.socket(socket.AF_UNIX)
conn.connect(sys.argv[1])
def upstream():
    while data := os.read(0, 65536):
        conn.sendall(data)
    conn.shutdown(socket.SHUT_WR)
threading.Thread(target=upstream, daemon=True).start()
while data := conn.recv(65536):
    os.write(1, data)
"""


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o700)
    return path


def _serve_sshd(socket_path: Path, config: Path, log: Path) -> Callable[[], None]:
    """Run `sshd -i` (the box's sshd) for each connection on a Unix socket.

    :returns: A function that stops listening and ends every sshd it started.
    """
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_path))
    server.listen()
    log_file = log.open("ab")
    children: list[subprocess.Popen[bytes]] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                log_file.close()
                return
            children.append(
                subprocess.Popen(
                    [_SSHD, "-i", "-e", "-f", str(config)],
                    stdin=conn.fileno(),
                    stdout=conn.fileno(),
                    stderr=log_file,
                )
            )
            conn.close()

    threading.Thread(target=accept, daemon=True).start()

    def stop() -> None:
        server.close()
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    return stop


def test_ssh_connect_signs_in_through_forward_and_starts_host(tmp_path: Path) -> None:
    """Prompts, port steering, the forwarded sign-in redirect and host start, end to end."""
    box = tmp_path / "box"
    laptop = tmp_path / "laptop"
    for directory in (box / "home", box / "bin", laptop / "home"):
        directory.mkdir(parents=True)
    with (
        isolated_local_server(tmp_path / "server") as base_url,
        HostLoginWorkspace(tmp_path / "workspace", base_url) as workspace,
        httpx.Client(base_url=base_url, headers={"X-Forwarded-Email": USER_EMAIL}) as client,
    ):
        box_env = isolated_process_env(
            box / "home",
            {
                "OMNIGENT_CONFIG_HOME": str(box / "home" / ".omnigent"),
                "OMNIGENT_NO_UPDATE_CHECK": "1",
                "DATABRICKS_CONFIG_FILE": str(box / "home" / ".databrickscfg"),
                "SSL_CERT_FILE": str(workspace.cert),
                "REQUESTS_CA_BUNDLE": str(workspace.cert),
                "BROWSER": None,
            },
            None,
        )
        box_env["PATH"] = os.pathsep.join(
            [
                str(box / "bin"),
                str(Path(shutil.which("databricks") or "").parent),
                "/usr/bin",
                "/bin",
            ]
        )
        omnigent = _write_executable(
            box / "bin" / "omnigent",
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_OMNIGENT_ENTRY)}"
            ' "$@"\n',
        )
        exports = "".join(
            f"export {name}={shlex.quote(value)}\n"
            for name, value in box_env.items()
            if name.isidentifier()
        )
        force_command = _write_executable(
            box / "session.sh", f'#!/bin/sh\n{exports}exec sh -c "$SSH_ORIGINAL_COMMAND"\n'
        )
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(box / "host_key")],
            check=True,
        )
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", _PASSPHRASE, "-f", str(laptop / "id")],
            check=True,
        )
        shutil.copy(laptop / "id.pub", box / "authorized_keys")
        sshd_config = box / "sshd_config"
        sshd_config.write_text(
            f"HostKey {box / 'host_key'}\n"
            f"AuthorizedKeysFile {box / 'authorized_keys'}\n"
            "PidFile none\nStrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            "ChallengeResponseAuthentication no\nAllowTcpForwarding yes\nLogLevel VERBOSE\n"
            f"ForceCommand {force_command}\n"
        )
        stop_sshd = _serve_sshd(box / "sshd.sock", sshd_config, tmp_path / "sshd.log")

        workspace_port = workspace.url.rsplit(":", 1)[1]
        relay = laptop / "relay.py"
        relay.write_text(_RELAY)
        ssh_config = laptop / "ssh_config"
        ssh_config.write_text(
            "Host box\n"
            f"  User {getpass.getuser()}\n"
            f"  ProxyCommand {sys.executable} {relay} {box / 'sshd.sock'}\n"
            f"  IdentityFile {laptop / 'id'}\n"
            "  IdentitiesOnly yes\n  IdentityAgent none\n"
            f"  UserKnownHostsFile {laptop / 'known_hosts'}\n"
            "  StrictHostKeyChecking ask\n"
            # A user's config may clear forwards, open them to the network, or make
            # every ssh a master; the app's must still work, on loopback, over one connection.
            "  ClearAllForwardings yes\n  GatewayPorts yes\n  ControlMaster yes\n"
        )
        ssh_config.chmod(0o600)
        laptop_env = {
            "PATH": os.environ["PATH"],
            "HOME": str(laptop / "home"),
            "SHELL": "/bin/sh",
            "NODE_EXTRA_CA_CERTS": str(workspace.cert),
            "TEST_KEY_PASSPHRASE": _PASSPHRASE,
            # OpenSSH before 8.4 ignores SSH_ASKPASS_REQUIRE and needs a DISPLAY.
            "DISPLAY": ":0",
        }
        try:
            run = subprocess.run(
                [
                    "unshare",
                    "-rn",
                    # No controlling terminal, so ssh asks the app, as from a GUI.
                    "setsid",
                    "-w",
                    "sh",
                    "-c",
                    'ip link set lo up && exec "$@"',
                    "laptop",
                    "node",
                    str(_DRIVER),
                    workspace.server_url,
                    "box",
                    str(ssh_config),
                    str(_BUSY_LAPTOP_PORT),
                    workspace_port,
                ],
                env=laptop_env,
                capture_output=True,
                text=True,
                timeout=200,
                check=False,
            )
            (tmp_path / "laptop-stderr.txt").write_text(run.stderr)
            events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
            (tmp_path / "laptop-events.json").write_text(json.dumps(events, indent=2))
            result = next((event["result"] for event in events if "result" in event), None)
            diagnostics = f"events={events}\nstderr={run.stderr[-3000:]}"
            assert result is not None and result["ok"] is True, diagnostics
            assert {"loopbackOnly": True} in events, diagnostics

            # Both prompts came to the app, once each: every later step reused the connection.
            prompts = [event["prompt"] for event in events if "prompt" in event]
            assert prompts == ["confirm", "secret"], diagnostics
            assert (laptop / "known_hosts").read_text().strip(), "trusted host key not saved"

            # The CLI avoided the laptop's busy port; the redirect came back through the forward.
            opened = next(event["open"] for event in events if "open" in event)
            redirect = httpx.URL(httpx.URL(opened).params["redirect_uri"])
            assert redirect.port not in (None, _BUSY_LAPTOP_PORT), opened
            browser = next(event for event in events if "browser" in event)
            assert browser == {"browser": 200, "landedPort": str(redirect.port)}
            assert workspace.authorizations == 1
            assert workspace.token_exchanges >= 1

            def host_online() -> bool:
                hosts = client.get("/v1/hosts").json()["hosts"]
                return any(
                    host["status"] == "online" and host["owner"] == USER_EMAIL for host in hosts
                )

            poll_until(host_online, timeout_s=30, interval_s=0.2)
            # The app reads the new host's id, so it can select it right away.
            host_ids = {host["host_id"] for host in client.get("/v1/hosts").json()["hosts"]}
            assert result["identity"]["hostId"] in host_ids, diagnostics
        finally:
            subprocess.run(
                [str(omnigent), "host", "stop", "--all"],
                env=box_env,
                capture_output=True,
                timeout=60,
                check=False,
            )
            stop_sshd()
