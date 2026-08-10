"""Tests for the Databricks Apps deploy orchestrator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_deploy_module():
    path = Path(__file__).parents[2] / "deploy" / "databricks" / "deploy.py"
    spec = importlib.util.spec_from_file_location("omnigent_databricks_deploy_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bundle_vars_include_managed_coda_configuration() -> None:
    deploy = _load_deploy_module()
    args = SimpleNamespace(
        app_name="omnigent",
        lakebase_branch="projects/omnigent/branches/production",
        lakebase_database=("projects/omnigent/branches/production/databases/databricks-postgres"),
        volume_name="main.omnigent.artifacts",
        otel_table_schema="main.omnigent_logs",
        coda_app_name="coda-main",
        coda_app_url="https://coda.example.com",
        omnigent_public_server_url="https://omnigent.example.com",
    )

    pairs = deploy._bundle_vars(args)

    assert "coda_app_name=coda-main" in pairs
    assert "coda_app_url=https://coda.example.com" in pairs
    assert "omnigent_public_server_url=https://omnigent.example.com" in pairs


def test_parse_args_rejects_partial_managed_coda_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy = _load_deploy_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deploy.py",
            "--app-name",
            "omnigent",
            "--lakebase-branch",
            "projects/omnigent/branches/production",
            "--lakebase-database",
            "projects/omnigent/branches/production/databases/databricks-postgres",
            "--volume-name",
            "main.omnigent.artifacts",
            "--coda-app-name",
            "coda-main",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        deploy._parse_args()
