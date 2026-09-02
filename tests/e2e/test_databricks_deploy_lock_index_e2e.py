"""Databricks deploy lock generation must not bake a machine mirror index.

Guards the operator journey: a machine whose uv configuration registers a
private mirror as the *default* index (``[[index]] ... default = true``) and
whose shell exports the generic ``UV_INDEX_URL`` at that mirror runs the
Databricks deploy. If either leaks into the generated
``deploy/databricks/src/uv.lock``, its ``registry`` sources and direct wheel
``url`` entries point at the mirror host, which the Databricks Apps build runtime
cannot reach, so the app install fails and the app never starts.

The test stands up two local "simple" indexes (a stand-in for public PyPI and
a stand-in for the machine mirror, both serving the same tiny wheel), points a
machine-level uv config and ``UV_INDEX_URL`` at the mirror, invokes the real
``run_uv_lock`` from ``deploy/databricks/deploy.py`` with ``DEPLOY_UV_INDEX_URL``
naming the intended index, and asserts the generated lock never references the
mirror host. When the lock resolved against the requested index, it also proves
the Apps-runtime half of the journey: with the mirror down, ``uv sync --locked``
still installs cleanly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="requires the uv CLI on PATH")


@pytest.fixture(scope="module")
def deploy_mod() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_lock_index", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _probe_wheel_bytes() -> bytes:
    """Build a minimal valid py3-none-any wheel for the probe package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("probepkg/__init__.py", "__version__ = '1.0.0'\n")
        z.writestr(
            "probepkg-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: probepkg\nVersion: 1.0.0\n\n",
        )
        z.writestr(
            "probepkg-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        )
        z.writestr("probepkg-1.0.0.dist-info/RECORD", "")
    return buf.getvalue()


def _write_simple_index(root: Path, wheel: bytes) -> None:
    """Lay out a PEP 503 "simple" index serving the probe wheel."""
    sha = hashlib.sha256(wheel).hexdigest()
    (root / "packages").mkdir(parents=True)
    (root / "packages" / "probepkg-1.0.0-py3-none-any.whl").write_bytes(wheel)
    project = root / "simple" / "probepkg"
    project.mkdir(parents=True)
    (project / "index.html").write_text(
        "<!DOCTYPE html><html><body>"
        f'<a href="../../packages/probepkg-1.0.0-py3-none-any.whl#sha256={sha}">'
        "probepkg-1.0.0-py3-none-any.whl</a></body></html>"
    )


@pytest.fixture
def index_servers(tmp_path: Path) -> Iterator[tuple[str, str, ThreadingHTTPServer]]:
    """Serve a public-PyPI stand-in and a machine-mirror stand-in over HTTP.

    :returns: ``(public_url, mirror_url, mirror_server)`` — the mirror server
        object is exposed so the test can take the mirror down, modelling the
        Databricks Apps build runtime where it is unreachable.
    """
    wheel = _probe_wheel_bytes()
    servers: list[ThreadingHTTPServer] = []
    urls: list[str] = []
    for side in ("public", "mirror"):
        root = tmp_path / side
        _write_simple_index(root, wheel)
        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        urls.append(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield urls[0], urls[1], servers[1]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_generated_app_lock_ignores_machine_mirror_index(
    deploy_mod: ModuleType,
    index_servers: tuple[str, str, ThreadingHTTPServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_url, mirror_url, mirror_server = index_servers

    # The operator's machine: uv config registers a private mirror as the
    # default index, which overrides --index-url unless the deploy opts out.
    machine_config = tmp_path / "xdg" / "uv"
    machine_config.mkdir(parents=True)
    (machine_config / "uv.toml").write_text(
        f'[[index]]\nurl = "{mirror_url}/simple"\ndefault = true\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in (
        "UV_CONFIG_FILE",
        "UV_NO_CONFIG",
        "UV_INDEX",
        "UV_DEFAULT_INDEX",
        "UV_EXTRA_INDEX_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    # Keep localhost traffic away from any ambient corporate proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    # Corporate shells export the generic UV_INDEX_URL at the mirror just to
    # make uv work locally; the deploy must not let it leak into the lock.
    monkeypatch.setenv("UV_INDEX_URL", f"{mirror_url}/simple")
    # Only the deploy-scoped variable opts the lock into a specific index; the
    # generated lock must point at this index, not the machine mirror.
    monkeypatch.setenv("DEPLOY_UV_INDEX_URL", f"{public_url}/simple")

    # The app source directory, shaped like deploy/databricks/src's generated
    # pyproject (same name / requires-python), with a registry dependency.
    src = tmp_path / "appsrc"
    src.mkdir()
    (src / "pyproject.toml").write_text(
        "[project]\n"
        'name = "omnigent-databricks-app"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        "dependencies = [\n"
        '  "probepkg==1.0.0",\n'
        "]\n"
    )

    deploy_mod.run_uv_lock(src)

    lock_text = (src / "uv.lock").read_text()
    # Match the /-terminated URL so an ephemeral mirror port that happens to
    # prefix the public port (e.g. :5001 vs :50011) cannot false-positive.
    leaked = [
        f"uv.lock line {number}: {line.strip()}"
        for number, line in enumerate(lock_text.splitlines(), start=1)
        if f"{mirror_url}/" in line
    ]
    assert not leaked, (
        "run_uv_lock baked the machine's mirror index into the generated app "
        f"uv.lock even though DEPLOY_UV_INDEX_URL requested {public_url}/simple; "
        "the Databricks Apps runtime cannot reach the mirror, so the deployed "
        "app fails to install. Leaked references:\n" + "\n".join(leaked)
    )

    # The fix pins the requested index, so the registry dependency must have
    # resolved from the public stand-in — anything else means the lock points
    # somewhere the Apps runtime was never promised to reach.
    assert f"{public_url}/simple" in lock_text, (
        "the generated lock does not reference the requested index "
        f"{public_url}/simple; it resolved from somewhere else entirely:\n" + lock_text
    )

    # The Databricks Apps build runtime: no machine uv config, and the
    # operator's mirror does not exist there. Installing from the generated
    # lock must still succeed.
    mirror_server.shutdown()
    mirror_server.server_close()
    empty_config = tmp_path / "xdg-empty"
    empty_config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_config))
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.delenv("DEPLOY_UV_INDEX_URL", raising=False)
    result = subprocess.run(
        ["uv", "sync", "--locked", "--python", "3.12"],
        cwd=src,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "installing the deploy-generated uv.lock in an environment without "
        "the operator's mirror failed (this is the Databricks Apps install "
        f"failure):\n{result.stdout}\n{result.stderr}"
    )
