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


def test_canvas_wheel_is_a_bundled_extension(deploy_mod: ModuleType, tmp_path: Path) -> None:
    wheels = [
        tmp_path / "omnigent-0.13.0-py3-none-any.whl",
        tmp_path / "omnigent_client-0.13.0-py3-none-any.whl",
        tmp_path / "omnigent_ui_sdk-0.13.0-py3-none-any.whl",
        tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl",
    ]

    core, extensions = deploy_mod._partition_built_wheels(wheels)

    assert [wheel.name for wheel in core] == [wheel.name for wheel in wheels[:3]]
    assert extensions == [wheels[3]]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes "])
def test_canvas_env_accepts_supported_truthy_values(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OMNIGENT_ENABLE_CANVAS", value)

    assert deploy_mod._env_var_is_truthy("OMNIGENT_ENABLE_CANVAS") is True


@pytest.mark.parametrize("value", ["", "0", "false", "on", "enabled"])
def test_canvas_env_is_disabled_for_other_values(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OMNIGENT_ENABLE_CANVAS", value)

    assert deploy_mod._env_var_is_truthy("OMNIGENT_ENABLE_CANVAS") is False


def test_canvas_env_is_disabled_when_unset(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_ENABLE_CANVAS", raising=False)

    assert deploy_mod._env_var_is_truthy("OMNIGENT_ENABLE_CANVAS") is False


def test_unknown_dist_wheel_is_not_installed_implicitly(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    wheel = tmp_path / "unrelated-1.0.0-py3-none-any.whl"

    with pytest.raises(SystemExit, match="--extension-wheel"):
        deploy_mod._partition_built_wheels([wheel])


def test_explicit_extension_may_be_reused_from_dist(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    wheel = tmp_path / "custom_extension-1.0.0-py3-none-any.whl"

    core, extensions = deploy_mod._partition_built_wheels([wheel], [wheel])

    assert core == []
    assert extensions == []


def test_stale_canvas_wheel_is_ignored_without_opt_in(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    canvas = tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl"

    selected = deploy_mod._select_extension_wheels(
        [canvas],
        [],
        enable_canvas=False,
    )

    assert selected == []


def test_canvas_opt_in_requires_exactly_one_wheel(deploy_mod: ModuleType) -> None:
    with pytest.raises(SystemExit, match="requires exactly one Canvas wheel"):
        deploy_mod._select_extension_wheels([], [], enable_canvas=True)


def test_canvas_opt_in_selects_built_wheel(deploy_mod: ModuleType, tmp_path: Path) -> None:
    canvas = tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl"

    selected = deploy_mod._select_extension_wheels(
        [canvas],
        [],
        enable_canvas=True,
    )

    assert selected == [canvas]


def test_explicit_canvas_wheel_in_dist_is_not_counted_twice(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    canvas = tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl"

    selected = deploy_mod._select_extension_wheels(
        [canvas],
        [canvas],
        enable_canvas=True,
    )

    assert selected == [canvas]


def test_explicit_canvas_wheel_cannot_bypass_env_opt_in(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    canvas = tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl"

    with pytest.raises(SystemExit, match="Canvas is disabled"):
        deploy_mod._select_extension_wheels([], [canvas], enable_canvas=False)


def test_other_explicit_extension_does_not_require_canvas_opt_in(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    custom = tmp_path / "custom_extension-1.0.0-py3-none-any.whl"

    selected = deploy_mod._select_extension_wheels(
        [],
        [custom],
        enable_canvas=False,
    )

    assert selected == [custom]


def test_canvas_wheel_is_locked_as_an_app_dependency(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    main = tmp_path / "omnigent-0.13.0-py3-none-any.whl"
    client = tmp_path / "omnigent_client-0.13.0-py3-none-any.whl"
    ui_sdk = tmp_path / "omnigent_ui_sdk-0.13.0-py3-none-any.whl"
    canvas = tmp_path / "omnigent_canvas-0.1.0-py3-none-any.whl"
    for wheel in (main, client, ui_sdk, canvas):
        wheel.write_bytes(b"wheel")

    pyproject = deploy_mod.build_uv_pyproject(
        main,
        [main, client, ui_sdk],
        [],
        "0.13.0",
        [canvas],
    )

    assert '"omnigent-canvas==0.1.0"' in pyproject
    assert 'omnigent-canvas = { path = "./omnigent_canvas-0.1.0-py3-none-any.whl" }' in pyproject
