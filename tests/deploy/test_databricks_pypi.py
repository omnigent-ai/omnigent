"""Databricks Apps dependency installation uses canonical public PyPI URLs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_pypi", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_uv_lock_normalizes_proxy_urls(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "normalize_uv_lock_registry.py").write_text(
        (_ROOT / "scripts" / "normalize_uv_lock_registry.py").read_text()
    )

    def fake_run(*args: object, **kwargs: object) -> None:
        del args
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "uv.lock").write_text(
            'source = { registry = "https://pypi-proxy.example/simple/" }\n'
            'sdist = { url = "https://pypi-proxy.example/packages/abc/pkg.tar.gz", '
            'hash = "sha256:abc", size = 123 }\n'
        )

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)

    deploy_mod.run_uv_lock(src)

    normalized = (src / "uv.lock").read_text()
    assert 'registry = "https://pypi.org/simple"' in normalized
    assert "https://files.pythonhosted.org/packages/abc/pkg.tar.gz" in normalized
    assert "size =" not in normalized


def test_app_overrides_workspace_package_proxy() -> None:
    source = (_ROOT / "deploy" / "databricks" / "src" / "app.yaml").read_text()
    assert "name: UV_INDEX_URL" in source
    assert "value: 'https://pypi.org/simple'" in source
