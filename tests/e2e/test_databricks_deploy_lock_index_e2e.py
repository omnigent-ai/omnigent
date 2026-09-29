"""Deploy locks isolate machine indexes while preserving explicit sources."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
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
import tomllib

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


def _wheel_bytes(name: str, dependencies: tuple[str, ...] = ()) -> bytes:
    """Build a minimal valid py3-none-any wheel for the named package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/__init__.py", "__version__ = '1.0.0'\n")
        z.writestr(
            f"{name}-1.0.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n"
            + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
            + "\n",
        )
        z.writestr(
            f"{name}-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        )
        z.writestr(f"{name}-1.0.0.dist-info/RECORD", "")
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
def index_servers(tmp_path: Path) -> Iterator[dict[str, tuple[str, ThreadingHTTPServer]]]:
    """Serve public, intentional custom, and machine-only indexes locally."""
    wheel = _wheel_bytes("probepkg")
    servers: dict[str, tuple[str, ThreadingHTTPServer]] = {}
    for side in ("public", "custom", "mirror"):
        root = tmp_path / side
        _write_simple_index(root, wheel)
        if side == "public":
            (root / "packages" / "urlpkg-1.0.0-py3-none-any.whl").write_bytes(
                _wheel_bytes("urlpkg")
            )
        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[side] = f"http://127.0.0.1:{server.server_address[1]}", server
    try:
        yield servers
    finally:
        for _, server in servers.values():
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    "use_custom_index", [False, True], ids=["public-default", "explicit-index"]
)
def test_generated_app_lock_preserves_explicit_sources(
    deploy_mod: ModuleType,
    index_servers: dict[str, tuple[str, ThreadingHTTPServer]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_custom_index: bool,
) -> None:
    public_url, _ = index_servers["public"]
    custom_url, _ = index_servers["custom"]
    mirror_url, mirror_server = index_servers["mirror"]
    selected_index = f"{custom_url if use_custom_index else public_url}/simple"

    # Default and extra machine indexes can outrank uv's index flags.
    machine_config = tmp_path / "xdg" / "uv"
    machine_config.mkdir(parents=True)
    config_file = machine_config / "uv.toml"
    config_file.write_text(
        f'[[index]]\nurl = "{mirror_url}/simple"\ndefault = true\n'
        f'\n[[index]]\nname = "corp-extra"\nurl = "{mirror_url}/simple"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    competing_env = {
        "UV_CONFIG_FILE": str(config_file),
        "UV_NO_CONFIG": "false",
        "UV_INDEX": f"{mirror_url}/simple",
        "UV_DEFAULT_INDEX": f"{mirror_url}/simple",
        "UV_EXTRA_INDEX_URL": f"{mirror_url}/simple",
        "UV_FIND_LINKS": f"{mirror_url}/packages",
    }
    for var, value in competing_env.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "lock-cache"))
    if use_custom_index:
        monkeypatch.setenv("UV_INDEX_URL", selected_index)
    else:
        monkeypatch.delenv("UV_INDEX_URL", raising=False)
    monkeypatch.setattr(deploy_mod, "_UV_DEFAULT_INDEX_URL", f"{public_url}/simple")

    # A local extension wheel may intentionally depend on a remote wheel URL.
    direct_url = f"{public_url}/packages/urlpkg-1.0.0-py3-none-any.whl"
    src = tmp_path / "appsrc"
    src.mkdir()
    local_wheel = "localpkg-1.0.0-py3-none-any.whl"
    (src / local_wheel).write_bytes(_wheel_bytes("localpkg", (f"urlpkg @ {direct_url}",)))
    (src / "pyproject.toml").write_text(
        "[project]\n"
        'name = "omnigent-databricks-app"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        "dependencies = [\n"
        '  "probepkg==1.0.0",\n'
        '  "localpkg",\n'
        "]\n\n"
        "[tool.uv.sources]\n"
        'localpkg = { path = "./localpkg-1.0.0-py3-none-any.whl" }\n'
    )

    deploy_mod.run_uv_lock(src)

    lock_text = (src / "uv.lock").read_text()
    assert f"{mirror_url}/" not in lock_text, "the machine-only mirror leaked into uv.lock"
    packages = {package["name"]: package for package in tomllib.loads(lock_text)["package"]}
    assert packages["probepkg"]["source"] == {"registry": selected_index}
    assert packages["localpkg"]["source"]["path"].removeprefix("./") == local_wheel
    assert packages["urlpkg"]["source"] == {"url": direct_url}

    # Install without the machine mirror or artifacts cached during locking.
    mirror_server.shutdown()
    mirror_server.server_close()
    empty_config = tmp_path / "xdg-empty"
    empty_config.mkdir()
    install_cache = tmp_path / "install-cache"
    assert not install_cache.exists()
    install_env = os.environ.copy()
    for var in (*competing_env, "UV_INDEX_URL"):
        install_env.pop(var, None)
    install_env["XDG_CONFIG_HOME"] = str(empty_config)
    install_env["UV_CACHE_DIR"] = str(install_cache)
    result = subprocess.run(
        [
            "uv",
            "sync",
            "--locked",
            "--python",
            "3.12",
            "--no-config",
            "--default-index",
            selected_index,
        ],
        cwd=src,
        env=install_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"fresh-cache install failed:\n{result.stdout}\n{result.stderr}"
    installed_python = src / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(installed_python), "-c", "import probepkg, localpkg, urlpkg"],
        cwd=src,
        env=install_env,
        check=True,
        timeout=10,
    )
