"""The Databricks Apps deploy ships the SPA outside the wheel.

Databricks Apps uploads the app source directory as Workspace files, and the
Workspace import API rejects any single file over 10 MB. The built SPA is ~25 MB
of assets, which takes the main wheel over that cap and fails the deploy before
anything is uploaded. So ``build.sh`` moves the SPA out of the wheel's package
data and the deploy ships it as loose files in ``src/web-ui/``, which
``src/app.py`` points the server at via ``OMNIGENT_WEB_UI_DIST``.

These tests pin each link in that chain, including the env var itself — the
server reads it at import time, so it is checked in a subprocess.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_DIR = _ROOT / "deploy" / "databricks"
_DEPLOY_PY = _DEPLOY_DIR / "deploy.py"
_BUILD_SH = _DEPLOY_DIR / "build.sh"
_APP_PY = _DEPLOY_DIR / "src" / "app.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_webui", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_spa(root: Path, *, asset_bytes: int = 32) -> Path:
    spa = root / "web-ui"
    (spa / "assets").mkdir(parents=True)
    (spa / "index.html").write_text("<!doctype html><title>omnigent</title>")
    (spa / "assets" / "index-abc123.js").write_bytes(b"x" * asset_bytes)
    return spa


def test_server_honours_web_ui_dist_env(tmp_path: Path) -> None:
    """The whole scheme hinges on OMNIGENT_WEB_UI_DIST being respected."""
    spa = _make_spa(tmp_path)
    code = "import omnigent.server.app as m; print(m._WEB_UI_DIST)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env={"PATH": "/usr/bin:/bin", "OMNIGENT_WEB_UI_DIST": str(spa)},
        check=True,
    )
    assert out.stdout.strip() == str(spa)


def test_app_py_points_server_at_loose_assets_before_importing_it() -> None:
    """The path is read at import time, so the setup must come first."""
    source = _APP_PY.read_text()
    setup_at = source.index('os.environ.setdefault("OMNIGENT_WEB_UI_DIST"')
    import_at = source.index("from omnigent.server.app import create_app")
    assert setup_at < import_at, "OMNIGENT_WEB_UI_DIST is set too late to take effect"
    assert 'Path(__file__).parent / "web-ui"' in source


def test_build_sh_moves_spa_out_of_the_wheel() -> None:
    source = _BUILD_SH.read_text()
    assert 'mv omnigent/server/static/web-ui "${WEB_UI_OUT_DIR}"' in source
    # The move has to happen while the SPA build is in scope, before uv build.
    assert source.index("WEB_UI_OUT_DIR") < source.index("uv build --wheel --out-dir dist/ .")


def test_build_sh_opts_the_backend_out_of_rebuilding_the_spa() -> None:
    """setup.py rebuilds the SPA into the package when the bundle is missing.

    Without the opt-out it puts the assets straight back into the wheel (and
    silently re-adds them to a SKIP_WEB_UI=1 wheel), so the export has to land
    before any wheel build.
    """
    source = _BUILD_SH.read_text()
    assert "export OMNIGENT_SKIP_WEB_UI=true" in source
    assert source.index("export OMNIGENT_SKIP_WEB_UI=true") < source.index(
        "uv build --wheel --out-dir dist/ sdks/python-client/"
    )


def test_setup_py_honours_the_web_ui_opt_out() -> None:
    """The opt-out build.sh relies on must keep working."""
    source = (_ROOT / "setup.py").read_text()
    assert 'os.environ.get("OMNIGENT_SKIP_WEB_UI") == "true"' in source


def test_build_wheels_requests_the_spa_outside_the_wheel(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        (tmp_path / "dist").mkdir(exist_ok=True)
        (tmp_path / "dist" / "omnigent-1.0-py3-none-any.whl").write_bytes(b"w")

    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    deploy_mod._build_wheels(skip_web_ui=False)
    assert captured["WEB_UI_OUT_DIR"] == str(tmp_path / "dist" / "web-ui")
    assert "SKIP_WEB_UI" not in captured

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=True)
    assert captured["SKIP_WEB_UI"] == "1"
    assert "WEB_UI_OUT_DIR" not in captured


def test_sync_src_web_ui_copies_build_into_app_source(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist)
    src = tmp_path / "deploy" / "databricks" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=False)

    assert (src / "web-ui" / "index.html").is_file()
    assert (src / "web-ui" / "assets" / "index-abc123.js").is_file()


def test_sync_src_web_ui_replaces_a_stale_copy(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Vite emits hashed chunk names; stale assets must not accumulate."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist)
    src = tmp_path / "src"
    (src / "web-ui" / "assets").mkdir(parents=True)
    (src / "web-ui" / "assets" / "index-STALE.js").write_text("old")
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=False)

    assert not (src / "web-ui" / "assets" / "index-STALE.js").exists()
    assert (src / "web-ui" / "assets" / "index-abc123.js").is_file()


def test_sync_src_web_ui_skip_web_ui_clears_assets(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An API-only deploy must not leave a previous SPA behind."""
    src = tmp_path / "src"
    _make_spa(src)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=True)

    assert not (src / "web-ui").exists()


def test_sync_src_web_ui_requires_a_build(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    with pytest.raises(SystemExit, match="no SPA build at"):
        deploy_mod._sync_src_web_ui(skip_web_ui=False)


def test_oversize_asset_fails_before_upload(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single Vite chunk over 10 MB would be rejected by the Workspace."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist, asset_bytes=deploy_mod._WORKSPACE_FILE_LIMIT_BYTES + 1)
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    with pytest.raises(SystemExit, match="over the 10 MB Workspace file cap"):
        deploy_mod._sync_src_web_ui(skip_web_ui=False)
    assert not (src / "web-ui").exists()
