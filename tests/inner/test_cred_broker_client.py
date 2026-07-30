"""The self-contained shim client (plan Task 9).

Invoked by absolute file path (as the real shims do), so it runs without the
omnigent package on sys.path.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[2] / "omnigent" / "inner" / "cred_broker_client.py"


@pytest.fixture
def short_tmp():
    """Short tmp dir so the AF_UNIX socket path stays under the sun_path limit."""
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _serve_once(sock_path: str, reply: dict, captured: dict) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                ch = conn.recv(4096)
                if not ch:
                    break
                buf += ch
            captured["request"] = json.loads(buf.decode())
            conn.sendall((json.dumps(reply) + "\n").encode())
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_client_injects_env_and_execs(tmp_path, short_tmp):
    sock = short_tmp / "b.sock"
    out = tmp_path / "seen.txt"
    fake = tmp_path / "faketool"
    fake.write_text(f'#!/bin/bash\necho "$PGPASSWORD" > "{out}"\n')
    fake.chmod(0o755)
    captured: dict = {}
    t = _serve_once(str(sock), {"env": {"PGPASSWORD": "tok"}, "binary": str(fake)}, captured)
    rc = subprocess.call(
        [sys.executable, str(CLIENT), "--socket", str(sock), "--tool", "psql", "--"],
        env={"OMNIGENT_CRED_BROKER_TOKEN": "T123", "PATH": "/usr/bin:/bin"},
    )
    t.join(timeout=5)
    assert rc == 0
    assert out.read_text().strip() == "tok"
    assert captured["request"] == {"tool": "psql", "token": "T123"}


def test_client_exits_on_broker_error(short_tmp):
    sock = short_tmp / "b.sock"
    captured: dict = {}
    t = _serve_once(str(sock), {"error": "denied"}, captured)
    rc = subprocess.call(
        [sys.executable, str(CLIENT), "--socket", str(sock), "--tool", "psql", "--"],
        env={"OMNIGENT_CRED_BROKER_TOKEN": "", "PATH": "/usr/bin:/bin"},
    )
    t.join(timeout=5)
    assert rc == 2
