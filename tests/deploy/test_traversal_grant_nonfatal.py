"""A failed UC traversal grant must not abort the deploy (#4999).

Both common failures are benign: the SP already has traversal via group
inheritance, or the deployer lacks MANAGE on a shared catalog. The
post-deploy app-boot smoke check is the real gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy" / "databricks" / "deploy.py"

spec = importlib.util.spec_from_file_location("deploy_mod", DEPLOY)
deploy = importlib.util.module_from_spec(spec)
sys.modules["deploy_mod"] = deploy
spec.loader.exec_module(deploy)


def _args():
    return argparse.Namespace(
        volume_name="main.omnigent.artifacts", profile=None
    )


def test_failed_grant_warns_and_continues(capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # CalledProcessError semantics are gone (check=False now); return failure.
        return types.SimpleNamespace(returncode=3, stdout="", stderr="Error: PERMISSION_DENIED")

    orig_run = deploy.subprocess.run
    deploy.subprocess.run = fake_run
    try:
        deploy._ensure_app_sp_uc_traversal(_args(), "sp-123")
    finally:
        deploy.subprocess.run = orig_run

    out = capsys.readouterr().out
    assert len(calls) == 2, "both grants attempted despite the first failing"
    assert "WARNING" in out and "PERMISSION_DENIED" in out
    assert "Continuing" in out
    # No raise happened (we got here).


def test_success_path_unchanged(capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    orig_run = deploy.subprocess.run
    deploy.subprocess.run = fake_run
    try:
        deploy._ensure_app_sp_traversal = deploy._ensure_app_sp_uc_traversal
        deploy._ensure_app_uc = deploy._ensure_app_sp_uc_traversal
        deploy._ensure_app_sp_uc_traversal(_args(), "sp-123")
    finally:
        deploy.subprocess.run = orig_run

    out = capsys.readouterr().out
    assert len(calls) == 2
    assert "WARNING" not in out


def test_missing_sp_still_skips(capsys):
    deploy._ensure_app_sp_uc_traversal(_args(), None)
    assert "skipping" in capsys.readouterr().out
