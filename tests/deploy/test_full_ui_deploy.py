"""Full-UI Databricks Apps deploys: oversize SPA stages loose, not aborts (#5003).

The ~25 MB SPA pushed the main wheel to ~10.9 MB (over the 10 MB cap) and the
deploy aborted to an API-only instruction. Now the SPA stages as loose files
under src/web-ui (each asset under the cap), the server reads them via the
OMNIGENT_WEB_UI_DIST override app.py already documents, and the rebuild fits.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy" / "databricks" / "deploy.py"

spec = importlib.util.spec_from_file_location("deploy_mod", DEPLOY)
deploy = importlib.util.module_from_spec(spec)
sys.modules["deploy_mod"] = deploy
spec.loader.exec_module(deploy)


@pytest.fixture()
def spa_build(tmp_path, monkeypatch):
    """Pretend the repo root is tmp_path with a built SPA + deploy/src."""
    (tmp_path / "omnigent" / "server" / "static" / "web-ui").mkdir(parents=True)
    (tmp_path / "omnigent" / "server" / "static" / "web-ui" / "index.html").write_text("<html>")
    (tmp_path / "omnigent" / "server" / "static" / "web-ui" / "app.js").write_text("console.log(1)")
    (tmp_path / "deploy" / "databricks" / "src").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy, "_deploy_dir", lambda: tmp_path / "deploy" / "databricks")
    return tmp_path


def test_stage_web_ui_copies_loose_files(spa_build):
    staged = deploy._stage_web_ui_as_loose_files()
    dest = spa_build / "deploy" / "databricks" / "src" / "web-ui"
    assert staged is True
    assert (dest / "index.html").read_text() == "<html>"
    assert (dest / "app.js").exists()


def test_stage_web_ui_noop_without_spa(tmp_path, monkeypatch):
    (tmp_path / "deploy" / "databricks" / "src").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy, "_deploy_dir", lambda: tmp_path / "deploy" / "databricks")
    assert deploy._stage_web_ui_as_loose_files() is False


def test_bundle_vars_pass_web_ui_dist_when_set():
    args = argparse.Namespace(
        app_name="omnigent",
        lakebase_branch="b",
        lakebase_database="d",
        volume_name="v",
        otel_table_schema="s",
        features="",
        web_ui_dist="src/web-ui",
    )
    flat = " ".join(deploy._bundle_vars(args))
    assert "web_ui_dist=src/web-ui" in flat


def test_bundle_vars_omit_web_ui_dist_by_default():
    args = argparse.Namespace(
        app_name="omnigent",
        lakebase_branch="b",
        lakebase_database="d",
        volume_name="v",
        otel_table_schema="s",
        features="",
    )
    flat = " ".join(deploy._bundle_vars(args))
    assert "web_ui_dist" not in flat
