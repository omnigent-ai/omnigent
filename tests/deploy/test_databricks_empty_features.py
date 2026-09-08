"""Empty Databricks Apps feature configuration remains deployable."""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_features", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(features: str) -> Namespace:
    return Namespace(
        app_name="omnigent",
        lakebase_branch="projects/omnigent/branches/production",
        lakebase_database="projects/omnigent/branches/production/databases/databricks-postgres",
        volume_name="main.omnigent.artifacts",
        otel_table_schema="main.omnigent_logs",
        features=features,
    )


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ("", " "),
        ("   ", " "),
        ("usage_page,harness_install", "usage_page,harness_install"),
    ],
)
def test_bundle_vars_provide_a_valid_empty_feature_source(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, features: str, expected: str
) -> None:
    monkeypatch.delenv("OMNIGENT_ENABLE_CANVAS", raising=False)

    values = deploy_mod._bundle_vars(_args(features))

    assert values[-2:] == ["--var", f"features={expected}"]


@pytest.mark.parametrize(
    ("switch", "features", "expected"),
    [
        ("true", "", "canvas"),
        ("1", "usage_page", "usage_page,canvas"),
        ("YES", "canvas,usage_page", "canvas,usage_page"),
        ("false", "usage_page", "usage_page"),
        ("0", "", " "),
    ],
)
def test_canvas_switch_adds_the_canvas_feature(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    switch: str,
    features: str,
    expected: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_ENABLE_CANVAS", switch)

    values = deploy_mod._bundle_vars(_args(features))

    assert values[-2:] == ["--var", f"features={expected}"]
