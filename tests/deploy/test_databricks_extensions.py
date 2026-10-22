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
    spec = importlib.util.spec_from_file_location("_databricks_deploy_extensions", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_core_wheels_are_selected_from_dist(deploy_mod: ModuleType, tmp_path: Path) -> None:
    wheels = [
        tmp_path / "omnigent-0.13.0-py3-none-any.whl",
        tmp_path / "omnigent_client-0.13.0-py3-none-any.whl",
        tmp_path / "omnigent_ui_sdk-0.13.0-py3-none-any.whl",
    ]

    assert deploy_mod._core_release_wheels(wheels) == wheels


def test_unknown_dist_wheel_is_not_installed_implicitly(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    wheel = tmp_path / "unrelated-1.0.0-py3-none-any.whl"

    with pytest.raises(SystemExit, match="--extension-wheel"):
        deploy_mod._core_release_wheels([wheel])


def test_explicit_extension_may_be_reused_from_dist(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    wheel = tmp_path / "custom_extension-1.0.0-py3-none-any.whl"

    assert deploy_mod._core_release_wheels([wheel], [wheel]) == []


def test_extension_wheel_is_locked_as_an_app_dependency(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    main = tmp_path / "omnigent-0.13.0-py3-none-any.whl"
    client = tmp_path / "omnigent_client-0.13.0-py3-none-any.whl"
    ui_sdk = tmp_path / "omnigent_ui_sdk-0.13.0-py3-none-any.whl"
    extension = tmp_path / "omnigent_hello_extension-0.1.0-py3-none-any.whl"
    for wheel in (main, client, ui_sdk, extension):
        wheel.write_bytes(b"wheel")

    pyproject = deploy_mod.build_uv_pyproject(
        main,
        [main, client, ui_sdk],
        [],
        "0.13.0",
        [extension],
    )

    assert '"omnigent-hello-extension==0.1.0"' in pyproject
    assert (
        'omnigent-hello-extension = { path = "./omnigent_hello_extension-0.1.0-py3-none-any.whl" }'
    ) in pyproject
