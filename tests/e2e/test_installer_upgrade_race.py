"""Concurrent installer runs must not corrupt a shared uv tool install.

Two hosts on one machine share one ``uv tool`` install of omnigent. When both
run the installer (``scripts/install_oss.sh --non-interactive``) for the same
new build at once — as two auto-upgrading hosts do when a server redeploy lands
on the same poll — every installer run must succeed and the shared install must
come out intact, even though live host processes keep executing from that
install (and keep writing ``__pycache__`` entries into it) the whole time.

The journey is hermetic: a stub ``omnigent`` distribution is served from a
loopback package index (standing in for the server the hosts install from),
and the shared install lives in an isolated ``UV_TOOL_DIR``.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install_oss.sh"

PACKAGE = "omnigent"
OLD_VERSION = "0.14.0"
NEW_VERSION = "0.14.1"

# The stub package mirrors the shape that makes the live install vulnerable: a
# large import graph, so every fresh CLI/runner process writes many .pyc files
# into the shared venv on first import.
N_MODULES = 400

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="requires uv on PATH")


def _build_stub_wheel(dest: Path, version: str) -> str:
    wheel_name = f"{PACKAGE}-{version}-py3-none-any.whl"
    records: list[tuple[str, str, str]] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        def add(path: str, data: bytes) -> None:
            zf.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append((path, f"sha256={digest}", str(len(data))))

        add(
            "omnigent/__init__.py",
            (
                "import importlib\n"
                "def main():\n"
                f"    for i in range({N_MODULES}):\n"
                "        try:\n"
                "            importlib.import_module(f'omnigent.pads.pad_{i:04d}')\n"
                "        except Exception:\n"
                "            return 1\n"
                f"    print('omnigent {version}')\n"
                "    return 0\n"
            ).encode(),
        )
        add("omnigent/pads/__init__.py", b"")
        for i in range(N_MODULES):
            body = f"VALUES_{i} = {list(range(50))!r}\n" * 8
            add(f"omnigent/pads/pad_{i:04d}.py", body.encode())
        di = f"{PACKAGE}-{version}.dist-info"
        add(
            f"{di}/METADATA",
            f"Metadata-Version: 2.1\nName: {PACKAGE}\nVersion: {version}\n".encode(),
        )
        add(
            f"{di}/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        add(
            f"{di}/entry_points.txt",
            b"[console_scripts]\nomnigent = omnigent:main\nomni = omnigent:main\n",
        )
        record = "".join(f"{p},{h},{s}\n" for p, h, s in records) + f"{di}/RECORD,,\n"
        zf.writestr(f"{di}/RECORD", record)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / wheel_name).write_bytes(buf.getvalue())
    return wheel_name


class _IndexHandler(http.server.BaseHTTPRequestHandler):
    """Minimal PEP 503 simple index over a directory of wheels."""

    wheels_dir: Path
    wheel_names: list[str] = []

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path == "/simple":
            self._send(
                200,
                "text/html",
                f'<html><body><a href="/simple/{PACKAGE}/">{PACKAGE}</a></body></html>'.encode(),
            )
        elif path == f"/simple/{PACKAGE}":
            links = "".join(f'<a href="/wheels/{w}">{w}</a><br/>' for w in self.wheel_names)
            self._send(200, "text/html", f"<html><body>{links}</body></html>".encode())
        elif self.path.startswith("/wheels/"):
            wheel = self.wheels_dir / self.path.split("/wheels/", 1)[1]
            if wheel.is_file():
                self._send(200, "application/octet-stream", wheel.read_bytes())
            else:
                self._send(404, "text/plain", b"not found")
        else:
            self._send(404, "text/plain", b"not found")

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def package_index(tmp_path: Path) -> Iterator[str]:
    wheels_dir = tmp_path / "wheels"
    _build_stub_wheel(wheels_dir, OLD_VERSION)
    _build_stub_wheel(wheels_dir, NEW_VERSION)
    _IndexHandler.wheels_dir = wheels_dir
    _IndexHandler.wheel_names = [f"{PACKAGE}-{OLD_VERSION}-py3-none-any.whl"]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IndexHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/simple"
    finally:
        server.shutdown()
        thread.join(timeout=10)


def _publish_new_build() -> None:
    _IndexHandler.wheel_names = [
        f"{PACKAGE}-{OLD_VERSION}-py3-none-any.whl",
        f"{PACKAGE}-{NEW_VERSION}-py3-none-any.whl",
    ]


def _install_env(tmp_path: Path, index_url: str) -> dict[str, str]:
    env = dict(os.environ)
    # The repo checkout on PYTHONPATH would shadow the installed stub package.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env.update(
        {
            "UV_TOOL_DIR": str(tmp_path / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(tmp_path / "uv-bin"),
            "UV_DEFAULT_INDEX": index_url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def _run_installer(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INSTALLER), "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


# Each iteration runs the installed CLI in a fresh process, the way hosts spawn
# runners and sessions from the shared install; every fresh process writes the
# .pyc files it finds missing.
_HOST_LOOP = """
import subprocess, sys, time
shim = sys.argv[1]
while True:
    try:
        subprocess.run([shim], capture_output=True, timeout=120)
    except Exception:
        pass
    time.sleep(0.02)
"""


def test_concurrent_upgrade_leaves_shared_install_working(
    tmp_path: Path, package_index: str
) -> None:
    env = _install_env(tmp_path, package_index)
    seed = subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--force",
            "-q",
            "--python",
            "3.12",
            f"{PACKAGE}=={OLD_VERSION}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert seed.returncode == 0, f"seeding the shared install failed: {seed.stderr}"

    shim = tmp_path / "uv-bin" / "omnigent"
    assert shim.exists(), "seed install produced no omnigent entry point"

    hosts = [
        subprocess.Popen(
            [sys.executable, "-c", _HOST_LOOP, str(shim)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(2)
    ]
    try:
        time.sleep(2.0)
        _publish_new_build()

        barrier = threading.Barrier(2)
        results: dict[str, subprocess.CompletedProcess[str]] = {}

        def fire(tag: str) -> None:
            barrier.wait()
            results[tag] = _run_installer(env)

        threads = [threading.Thread(target=fire, args=(tag,)) for tag in ("host-A", "host-B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for host in hosts:
            host.terminate()
        for host in hosts:
            try:
                host.wait(timeout=10)
            except subprocess.TimeoutExpired:
                host.kill()

    failures = {
        tag: (r.returncode, (r.stdout + r.stderr).strip().splitlines()[-3:])
        for tag, r in sorted(results.items())
        if r.returncode != 0
    }
    assert not failures, (
        "concurrent installer runs into one shared uv tool install must all "
        f"succeed, but failed: {failures}"
    )

    tool_env = tmp_path / "uv-tools" / PACKAGE
    assert (tool_env / "bin").is_dir(), (
        "the shared install lost its bin/ directory; the hosts' launchd/python "
        f"entry points are gone (env now contains: "
        f"{sorted(p.name for p in tool_env.iterdir()) if tool_env.is_dir() else 'nothing'})"
    )

    cli = subprocess.run([str(shim)], env=env, capture_output=True, text=True, timeout=120)
    assert cli.returncode == 0, (
        "the shared omnigent CLI is broken after the concurrent upgrade "
        f"(rc={cli.returncode}, stderr tail: {cli.stderr.strip()[-300:]!r}); "
        "a host restart would not survive"
    )
    assert f"omnigent {NEW_VERSION}" in cli.stdout, (
        f"the upgrade never landed: CLI reports {cli.stdout.strip()!r} instead "
        f"of version {NEW_VERSION}"
    )
