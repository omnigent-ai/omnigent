"""A failed Unity Catalog traversal grant must not abort a Databricks deploy.

``_ensure_app_sp_uc_traversal`` grants ``USE_CATALOG`` / ``USE_SCHEMA`` to the
app service principal on the artifact volume's parents. Both grants routinely
cannot be applied — the SP frequently already has traversal through group
inheritance, and a deployer without ``MANAGE`` on a shared catalog is not
allowed to add it — yet the app boots fine in both cases. Letting the grant
abort the deploy therefore fails workspaces that would have worked.

The guard is deliberately narrow, so these tests pin both directions:

- a ``CalledProcessError`` from the grant subprocess warns and continues;
- an unrelated ``CalledProcessError`` (another deploy step) still propagates;
- a non-``CalledProcessError`` from the grant subprocess still propagates, so a
  missing ``databricks`` CLI or a timeout is never silently reported as a
  permission warning;
- the warning is bounded and secret-scrubbed, because the CLI can echo a token
  or an ``Authorization`` header back inside an error and the deploy log is
  routinely a CI transcript.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"

_REQUIRED_ARGS = [
    "--app-name",
    "omnigent",
    "--lakebase-branch",
    "projects/omnigent/branches/production",
    "--lakebase-database",
    "projects/omnigent/branches/production/databases/databricks-postgres",
    "--volume-name",
    "main.omnigent.artifacts",
]


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    """Load ``deploy.py`` by path — ``deploy/`` is not an installed package."""
    spec = importlib.util.spec_from_file_location("_databricks_deploy_uc", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse(deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, *extra: str) -> Any:
    monkeypatch.setattr(sys, "argv", ["deploy.py", *_REQUIRED_ARGS, *extra])
    return deploy_mod._parse_args()


def test_uc_grant_permission_failure_warns_and_continues(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both grants failing must warn per grant and return normally."""
    args = _parse(deploy_mod, monkeypatch)
    attempted: list[str] = []

    def fake_run(cmd: list[str], **_: object) -> None:
        attempted.append(cmd[4])
        raise subprocess.CalledProcessError(
            1, cmd, stderr="PERMISSION_DENIED: requires MANAGE on catalog\n"
        )

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    # Must not raise: the app-boot smoke check later in main() is the real gate.
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    # The second grant is still attempted after the first one fails.
    assert attempted == ["main", "main.omnigent"]

    out = capsys.readouterr().out
    assert "warning: USE_CATALOG grant on main failed" in out
    assert "warning: USE_SCHEMA grant on main.omnigent failed" in out
    assert "PERMISSION_DENIED" in out


@pytest.mark.parametrize(
    ("stderr", "must_not_contain", "must_contain"),
    [
        pytest.param(
            "Error: default auth: token=dapiSUPERSECRETVALUE host=https://private.example.com",
            ["dapiSUPERSECRETVALUE"],
            ["<redacted>"],
            id="config-dump-token",
        ),
        pytest.param(
            "Error: default auth: DATABRICKS_TOKEN=dapiSUPERSECRETVALUE host=https://wsp.example.com",
            ["dapiSUPERSECRETVALUE"],
            ["<redacted>"],
            id="underscore-prefixed-env-name",
        ),
        pytest.param(
            "DATABRICKS_CLIENT_SECRET=SUPERSECRETVALUE not accepted",
            ["SUPERSECRETVALUE"],
            ["<redacted>"],
            id="underscore-prefixed-client-secret",
        ),
        pytest.param(
            "x-api-key: SUPERSECRETAPIKEY rejected",
            ["SUPERSECRETAPIKEY"],
            ["<redacted>"],
            id="api-key-header",
        ),
        pytest.param(
            "transport error: authorization: Bearer eyJhbGSUPERSECRETJWT.payload.sig",
            ["eyJhbGSUPERSECRETJWT"],
            ["<redacted>"],
            id="bearer-header",
        ),
        pytest.param(
            "failed: client_secret=SUPERSECRETCLIENTSECRET",
            ["SUPERSECRETCLIENTSECRET"],
            ["<redacted>"],
            id="client-secret",
        ),
        pytest.param(
            "PERMISSION_DENIED: requires MANAGE on catalog",
            ["<redacted>"],
            ["PERMISSION_DENIED: requires MANAGE on catalog"],
            id="benign-message-kept-verbatim",
        ),
    ],
)
def test_grant_warning_is_scrubbed_and_bounded(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
    must_not_contain: list[str],
    must_contain: list[str],
) -> None:
    args = _parse(deploy_mod, monkeypatch)

    def fake_run(cmd: list[str], **_: object) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr=stderr)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    out = capsys.readouterr().out
    for secret in must_not_contain:
        assert secret not in out, out
    for expected in must_contain:
        assert expected in out, out


def test_grant_warning_is_single_line_and_truncated(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A chatty CLI must not dump a stack trace into the deploy log."""
    args = _parse(deploy_mod, monkeypatch)
    noisy = "first line of the failure " + "x" * 500 + "\nsecond line\nthird line"

    def fake_run(cmd: list[str], **_: object) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr=noisy)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    out = capsys.readouterr().out
    assert "second line" not in out
    assert "third line" not in out
    # Two grants, each one line.
    warnings = [line for line in out.splitlines() if "warning:" in line]
    assert len(warnings) == 2
    for line in warnings:
        assert len(line) < 320, len(line)


def test_uc_grant_failure_without_stderr_reports_returncode(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silent CLI failure still produces an actionable warning."""
    args = _parse(deploy_mod, monkeypatch)

    def fake_run(cmd: list[str], **_: object) -> None:
        raise subprocess.CalledProcessError(7, cmd, stderr=None)

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    assert "rc=7" in capsys.readouterr().out


def test_unrelated_called_process_error_still_propagates(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The guard is scoped to the grant call, not to deploys in general.

    ``run_uv_lock`` is another ``check=True`` deploy step. Its
    ``CalledProcessError`` must still abort the deploy — a broken dependency
    resolution is not something to warn past.
    """
    _parse(deploy_mod, monkeypatch)

    def fake_run(cmd: list[str], **_: object) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr="uv lock exploded")

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        deploy_mod.run_uv_lock(tmp_path)


def test_grant_non_called_process_error_still_propagates(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing CLI or a timeout must not be swallowed as a grant warning."""
    args = _parse(deploy_mod, monkeypatch)

    def missing_cli(cmd: list[str], **_: object) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'databricks'")

    monkeypatch.setattr(deploy_mod.subprocess, "run", missing_cli)
    with pytest.raises(FileNotFoundError):
        deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    def timed_out(cmd: list[str], **_: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(deploy_mod.subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")


def test_grant_success_path_is_unchanged(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A working grant emits no warning and still issues both grants."""
    args = _parse(deploy_mod, monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        calls.append(cmd)
        assert kwargs["check"] is True

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    assert [cmd[3] for cmd in calls] == ["catalog", "schema"]
    assert "warning" not in capsys.readouterr().out
