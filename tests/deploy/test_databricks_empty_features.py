"""Empty Databricks Apps feature configuration remains deployable."""

from __future__ import annotations

import ast
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


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ("", " "),
        ("   ", " "),
        ("usage_page,harness_install", "usage_page,harness_install"),
    ],
)
def test_bundle_env_provides_a_valid_feature_source(
    deploy_mod: ModuleType, features: str, expected: str
) -> None:
    args = Namespace(
        app_name="omnigent",
        lakebase_branch="projects/omnigent/branches/production",
        lakebase_database="projects/omnigent/branches/production/databases/databricks-postgres",
        volume_name="main.omnigent.artifacts",
        otel_table_schema="main.omnigent_logs",
        features=features,
    )

    # `--var` splits on commas, so features must travel through the environment.
    assert not any(value.startswith("features=") for value in deploy_mod._bundle_vars(args))
    assert deploy_mod._bundle_env(args)["BUNDLE_VAR_features"] == expected


def _is_call_to(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def test_every_bundle_invocation_passes_the_feature_env() -> None:
    """Each `databricks bundle` subprocess that spreads _bundle_vars must also pass _bundle_env."""
    bundle_calls = [
        node
        for node in ast.walk(ast.parse(_DEPLOY_PY.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and any(
            isinstance(element, ast.Starred) and _is_call_to(element.value, "_bundle_vars")
            for argument in node.args
            if isinstance(argument, ast.List)
            for element in argument.elts
        )
    ]
    # bind, deploy, and run.
    assert len(bundle_calls) == 3
    for call in bundle_calls:
        env = next((kw.value for kw in call.keywords if kw.arg == "env"), None)
        assert env is not None and _is_call_to(env, "_bundle_env"), (
            f"bundle call at line {call.lineno} does not pass env=_bundle_env(args)"
        )
