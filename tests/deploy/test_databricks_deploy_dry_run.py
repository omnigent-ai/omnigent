"""Guard: `deploy.py --dry-run` resolves a plan but mutates nothing.

The dry-run path must reuse the real version/config resolution and
print a plan, then exit 0 before any build, file write, subprocess, or
SDK/API call happens.
"""

from __future__ import annotations

import argparse
import types
from importlib import import_module
from pathlib import Path

import pytest

_deploy = import_module("deploy.databricks.deploy")


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "app_name": "omnigent",
        "lakebase_branch": "projects/omnigent/branches/production",
        "lakebase_database": "projects/omnigent/branches/production/databases/db",
        "volume_name": "main.omnigent.artifacts",
        "compute_size": "LARGE",
        "otel_table_schema": "main.omnigent_logs",
        "features": "",
        "target": "prod",
        "profile": None,
        "version": None,
        "dry_run": True,
        "skip_build": False,
        "skip_web_ui": False,
        "app_url": None,
        "no_smoke_check": False,
        "keep_version_bump": False,
        "allow_dirty": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def _no_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blow up if dry-run reaches anything that builds, writes, or calls out."""

    def _boom_subprocess(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess.run must not run in dry-run mode")

    monkeypatch.setattr(_deploy.subprocess, "run", _boom_subprocess)

    for name in (
        "_clear_env_vars",
        "_assert_clean_tree",
        "_clean_build_artifacts",
        "_stamp_versions",
        "_build_wheels",
        "_ensure_bound",
        "_ensure_compute_size",
        "_ensure_app_sp_uc_traversal",
        "_smoke_check",
        "write_uv_dependency_files",
    ):

        def _make(fn_name: str) -> object:
            def _boom(*_a: object, **_k: object) -> None:
                raise AssertionError(f"{fn_name} must not run in dry-run mode")

            return _boom

        monkeypatch.setattr(_deploy, name, _make(name))


def test_dry_run_makes_no_api_calls_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_mutations: None,
) -> None:
    """No WorkspaceClient is constructed, and the command exits 0."""

    def _boom_client(*_a: object, **_k: object) -> None:
        raise AssertionError("WorkspaceClient must not be constructed in dry-run mode")

    # The SDK is late-imported inside main(), so stub the module the
    # import would resolve to rather than patching an attribute.
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = _boom_client  # type: ignore[attr-defined]
    pkg = types.ModuleType("databricks")
    pkg.sdk = sdk  # type: ignore[attr-defined]
    monkeypatch.setitem(_deploy.sys.modules, "databricks", pkg)
    monkeypatch.setitem(_deploy.sys.modules, "databricks.sdk", sdk)

    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args())

    assert _deploy.main() == 0
    assert capsys.readouterr().out


@pytest.mark.parametrize("volume_name", ["catalog.schema", "catalog..volume", ".schema.volume"])
def test_dry_run_rejects_malformed_volume_before_any_operation(
    monkeypatch: pytest.MonkeyPatch,
    _no_mutations: None,
    volume_name: str,
) -> None:
    monkeypatch.setattr(
        _deploy,
        "_parse_args",
        lambda: _args(volume_name=volume_name),
    )

    with pytest.raises(SystemExit, match=r"catalog\.schema\.volume"):
        _deploy.main()


def test_dry_run_prints_resolved_version_and_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_mutations: None,
) -> None:
    """The table carries the resolved version, bundle vars, and ordered steps."""
    monkeypatch.setattr(_deploy, "_read_base_version", lambda: "0.1.0")
    monkeypatch.setattr(_deploy, "_compute_deploy_version", lambda base, explicit: "0.1.0.post42")
    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args())

    assert _deploy.main() == 0
    out = capsys.readouterr().out

    assert "dry run" in out.lower()
    assert "0.1.0.post42" in out
    assert "app_name" in out and "omnigent" in out
    assert "volume_name" in out and "main.omnigent.artifacts" in out
    assert "bundle deploy --target prod" in out
    assert "bundle run omnigent --target prod" in out


def test_dry_run_reuses_wheel_version_when_skipping_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    _no_mutations: None,
) -> None:
    """--skip-build derives the version from dist/ instead of stamping a new one."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for prefix in _deploy._WHEEL_PREFIXES:
        (dist / f"{prefix}0.1.0.post7-py3-none-any.whl").touch()

    monkeypatch.setattr(_deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_deploy, "_read_base_version", lambda: "0.1.0")
    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args(skip_build=True))

    assert _deploy.main() == 0
    out = capsys.readouterr().out
    assert "0.1.0.post7" in out
    assert "reuse dist/ wheels" in out


def test_dry_run_rejects_version_mismatch_with_reused_wheels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _no_mutations: None,
) -> None:
    """An explicit --version that contradicts dist/ fails in dry-run too."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for prefix in _deploy._WHEEL_PREFIXES:
        (dist / f"{prefix}0.1.0.post7-py3-none-any.whl").touch()

    monkeypatch.setattr(_deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_deploy, "_read_base_version", lambda: "0.1.0")
    monkeypatch.setattr(
        _deploy, "_parse_args", lambda: _args(skip_build=True, version="0.1.0.post9")
    )

    with pytest.raises(SystemExit, match="does not match reused wheel version"):
        _deploy.main()


@pytest.mark.parametrize(
    ("size", "should_pass"),
    [
        (_deploy._WORKSPACE_WHEEL_LIMIT_BYTES, True),
        (_deploy._WORKSPACE_WHEEL_LIMIT_BYTES + 1, False),
    ],
)
def test_skip_build_dry_run_applies_real_wheel_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _no_mutations: None,
    size: int,
    should_pass: bool,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    for index, prefix in enumerate(_deploy._WHEEL_PREFIXES):
        wheel = dist / f"{prefix}0.1.0.post7-py3-none-any.whl"
        wheel.touch()
        if index == 0:
            with wheel.open("wb") as handle:
                handle.truncate(size)
    monkeypatch.setattr(_deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_deploy, "_read_base_version", lambda: "0.1.0")
    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args(skip_build=True))

    if should_pass:
        assert _deploy.main() == 0
    else:
        with pytest.raises(SystemExit, match="over 10 MB"):
            _deploy.main()


def test_dry_run_omits_smoke_check_step_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_mutations: None,
) -> None:
    """The plan reflects --no-smoke-check, so it matches the real run's steps."""
    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args(no_smoke_check=True))

    assert _deploy.main() == 0
    assert "smoke-check" not in capsys.readouterr().out


def test_without_dry_run_the_real_path_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanity check that --dry-run is opt-in: omitting it reaches the deploy."""
    reached: list[str] = []
    monkeypatch.setattr(_deploy, "_clear_env_vars", lambda: reached.append("env"))
    monkeypatch.setattr(_deploy, "_assert_clean_tree", lambda skip: reached.append("tree"))
    monkeypatch.setattr(_deploy, "_read_base_version", lambda: "0.1.0")
    monkeypatch.setattr(_deploy, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_deploy, "_clean_build_artifacts", lambda: reached.append("clean"))
    monkeypatch.setattr(_deploy, "_stamp_versions", lambda version: {})

    def _boom_build(**_k: object) -> None:
        reached.append("build")
        raise SystemExit("stop here")

    monkeypatch.setattr(_deploy, "_build_wheels", _boom_build)
    monkeypatch.setattr(_deploy, "_parse_args", lambda: _args(dry_run=False))

    with pytest.raises(SystemExit):
        _deploy.main()
    assert reached == ["env", "tree", "clean", "build"]
