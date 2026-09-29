"""Deployment index selection must not inherit competing machine settings."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"

_PUBLIC = "https://pypi.org/simple"


@pytest.fixture(scope="module")
def deploy_mod() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_lock_unit", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(autouse=True)
def _clean_index_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient index config so a mirror-configured machine can't skew us."""
    for var in (
        "UV_CONFIG_FILE",
        "UV_DEFAULT_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_NO_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def lock_call(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run ``run_uv_lock`` with a stubbed uv and capture the subprocess call."""
    call: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        call["cmd"] = cmd
        call.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    call["src"] = tmp_path
    return call


@pytest.mark.parametrize("index_url", [None, "", "https://packages.example/simple/"])
def test_lock_uses_selected_index(
    deploy_mod: ModuleType,
    lock_call: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    index_url: str | None,
) -> None:
    if index_url is not None:
        monkeypatch.setenv("UV_INDEX_URL", index_url)

    deploy_mod.run_uv_lock(lock_call["src"])

    assert lock_call["cmd"] == [
        "uv",
        "lock",
        "--python",
        "3.12",
        "--no-config",
        "--default-index",
        index_url or _PUBLIC,
    ]
    assert lock_call["cwd"] == lock_call["src"]
    assert lock_call["check"] is True
    assert "UV_INDEX_URL" not in lock_call["env"]


@pytest.mark.parametrize("index_url", [None, "https://packages.example/simple"])
def test_lock_ignores_competing_index_env_without_changing_parent(
    deploy_mod: ModuleType,
    lock_call: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    index_url: str | None,
) -> None:
    """Extra indexes survive --no-config and outrank the selected default."""
    hostile = {
        "UV_CONFIG_FILE": "/etc/uv/uv.toml",
        "UV_DEFAULT_INDEX": "https://mirror.corp.example/simple",
        "UV_EXTRA_INDEX_URL": "https://mirror.corp.example/simple",
        "UV_FIND_LINKS": "https://mirror.corp.example/wheels",
        "UV_INDEX": "corp = https://mirror.corp.example/simple",
        "UV_NO_CONFIG": "0",
    }
    if index_url is not None:
        monkeypatch.setenv("UV_INDEX_URL", index_url)
    for var, value in hostile.items():
        monkeypatch.setenv(var, value)

    deploy_mod.run_uv_lock(lock_call["src"])

    for var in hostile:
        assert var not in lock_call["env"]
        assert os.environ[var] == hostile[var]
    index = lock_call["cmd"][lock_call["cmd"].index("--default-index") + 1]
    assert index == (index_url or _PUBLIC)
    assert os.environ.get("UV_INDEX_URL") == index_url


def test_lock_preserves_non_index_environment(
    deploy_mod: ModuleType, lock_call: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "UV_NATIVE_TLS": "true",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
        "UV_HTTP_TIMEOUT": "120",
    }
    for var, value in settings.items():
        monkeypatch.setenv(var, value)

    deploy_mod.run_uv_lock(lock_call["src"])

    for var, value in settings.items():
        assert lock_call["env"][var] == value


@pytest.mark.parametrize(
    ("index_url", "shown"),
    [
        (
            "https://user:p@ssw0rd@packages.example/simple?token=qu3ry-tok3n#frag",
            "https://***@packages.example/simple",
        ),
        (
            "https://packages.example/simple?token=qu3ry-tok3n#frag",
            "https://packages.example/simple",
        ),
    ],
)
def test_lock_redacts_credentials_in_log_but_passes_them_to_uv(
    deploy_mod: ModuleType,
    lock_call: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    index_url: str,
    shown: str,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", index_url)

    deploy_mod.run_uv_lock(lock_call["src"])

    assert lock_call["cmd"][-1] == index_url
    assert capsys.readouterr().out == (
        f"[deploy] uv lock --python 3.12 --no-config --default-index {shown}\n"
    )


def test_lock_propagates_uv_failure_without_index_credentials(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_url = "https://user:p@ssw0rd@packages.example/simple?token=qu3ry-tok3n#frag"
    monkeypatch.setenv("UV_INDEX_URL", index_url)

    def fail_run(cmd: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fail_run)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        deploy_mod.run_uv_lock(tmp_path)

    assert excinfo.value.returncode == 1
    assert excinfo.value.cmd[-1] == "https://***@packages.example/simple"
    diagnostics = repr(excinfo.value) + "".join(traceback.format_exception(excinfo.value))
    for secret in ("user", "p@ssw0rd", "qu3ry-tok3n", "frag"):
        assert secret not in diagnostics
