"""The deploy's app lock must resolve only from public PyPI.

Ambient uv index variables and machine config must not override that index.
A lock resolved from another registry must fail before deployment.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
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
        call["env"] = kwargs["env"]
        # A canonical lock, so the post-lock registry check passes.
        (tmp_path / "uv.lock").write_text(f'source = {{ registry = "{_PUBLIC}" }}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    call["src"] = tmp_path
    return call


def test_lock_ignores_generic_uv_index_url(
    deploy_mod: ModuleType, lock_call: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell-wide UV_INDEX_URL mirror must not become the app lock's index."""
    monkeypatch.setenv("UV_INDEX_URL", "https://mirror.corp.example/simple")

    deploy_mod.run_uv_lock(lock_call["src"])

    assert "--default-index" in lock_call["cmd"]
    index = lock_call["cmd"][lock_call["cmd"].index("--default-index") + 1]
    assert index == _PUBLIC
    assert "UV_INDEX_URL" not in lock_call["env"]


def test_lock_shuts_out_machine_uv_config(
    deploy_mod: ModuleType, lock_call: dict[str, Any]
) -> None:
    """Machine-level uv config outranks the index flags, so uv must not read it."""
    deploy_mod.run_uv_lock(lock_call["src"])

    assert "--no-config" in lock_call["cmd"]
    # --index-url is the deprecated weak form a config default index outranks.
    assert "--index-url" not in lock_call["cmd"]


def test_lock_strips_every_index_env_var(
    deploy_mod: ModuleType, lock_call: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index env vars survive --no-config, so none may reach the child env."""
    hostile = {
        "UV_CONFIG_FILE": "/etc/uv/uv.toml",
        "UV_DEFAULT_INDEX": "https://mirror.corp.example/simple",
        "UV_EXTRA_INDEX_URL": "https://mirror.corp.example/simple",
        "UV_FIND_LINKS": "https://mirror.corp.example/wheels",
        "UV_INDEX": "corp = https://mirror.corp.example/simple",
        "UV_INDEX_URL": "https://mirror.corp.example/simple",
        "UV_NO_CONFIG": "0",
    }
    for var, value in hostile.items():
        monkeypatch.setenv(var, value)

    deploy_mod.run_uv_lock(lock_call["src"])

    for var in hostile:
        assert var not in lock_call["env"]
    index = lock_call["cmd"][lock_call["cmd"].index("--default-index") + 1]
    assert index == _PUBLIC


def test_lock_fails_loudly_on_foreign_registry(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that still resolved from another host must abort the deploy."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        (tmp_path / "uv.lock").write_text(
            f'source = {{ registry = "{_PUBLIC}" }}\n'
            'source = { registry = "https://mirror.corp.example/simple" }\n'
        )
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match=r"mirror\.corp\.example"):
        deploy_mod.run_uv_lock(tmp_path)


def test_registry_check_rejects_direct_url_sources(deploy_mod: ModuleType, tmp_path: Path) -> None:
    """A remote direct-URL source is a leak; local path sources are fine."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        f'source = {{ registry = "{_PUBLIC}" }}\n'
        'source = { url = "https://mirror.corp.example/wheels/pkg-1.0-py3-none-any.whl" }\n'
        'source = { path = "./omnigent-0.1.0-py3-none-any.whl" }\n'
    )

    with pytest.raises(SystemExit, match=r"mirror\.corp\.example"):
        deploy_mod._check_lock_registries(lock, _PUBLIC)


def test_registry_check_redacts_index_credentials(deploy_mod: ModuleType, tmp_path: Path) -> None:
    """Index URLs with embedded credentials must not reach the error message."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://user:secret@mirror.corp.example/simple" }\n'
        'source = { url = "https://downloads.example/pkg.whl?token=qu3ry-tok3n#frag" }\n'
    )

    with pytest.raises(SystemExit) as excinfo:
        deploy_mod._check_lock_registries(lock, "https://tok3n@proxy.example/simple")

    message = str(excinfo.value)
    assert "secret" not in message
    assert "tok3n" not in message
    assert "***@mirror.corp.example" in message
    assert "https://downloads.example/pkg.whl" in message


def test_registry_check_tolerates_trailing_slash(deploy_mod: ModuleType, tmp_path: Path) -> None:
    """Equivalent registry URLs differing only by a trailing slash are canonical."""
    lock = tmp_path / "uv.lock"
    lock.write_text(f'source = {{ registry = "{_PUBLIC}/" }}\n')

    deploy_mod._check_lock_registries(lock, _PUBLIC)  # must not raise


def test_registry_check_matches_credentialed_index(deploy_mod: ModuleType, tmp_path: Path) -> None:
    """A credentialed index must match its own credential-less lock entry.

    uv does not persist index userinfo into ``uv.lock`` registry sources, so a
    lock requested against ``https://user:token@proxy/simple`` records
    ``https://proxy/simple`` — a correct lock that must not abort the deploy.
    """
    lock = tmp_path / "uv.lock"
    lock.write_text('source = { registry = "https://proxy.example/simple" }\n')

    deploy_mod._check_lock_registries(
        lock, "https://user:tok3n@proxy.example/simple"
    )  # must not raise


def test_redact_url_handles_literal_at_in_password(deploy_mod: ModuleType) -> None:
    """A password containing a literal `@` must still be fully redacted."""
    redacted = deploy_mod._redact_url("https://user:p@ssw0rd@mirror.corp.example/simple")

    assert redacted == "https://***@mirror.corp.example/simple"
    assert "ssw0rd" not in redacted


def test_redact_url_drops_query_and_fragment(deploy_mod: ModuleType) -> None:
    """Query strings and fragments can carry tokens; diagnostics must drop them."""
    redacted = deploy_mod._redact_url("https://downloads.example/pkg.whl?token=qu3ry-tok3n#frag")

    assert redacted == "https://downloads.example/pkg.whl"
