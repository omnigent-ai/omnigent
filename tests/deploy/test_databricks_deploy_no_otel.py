"""``--no-otel`` must resolve to a genuinely uninstrumented deploy target.

A workspace with no OTel collector and no UC OTel tables cannot deploy Omnigent
to Databricks Apps cleanly: the app runs under ``opentelemetry-instrument`` with
``OTEL_TRACES_SAMPLER=always_on``, so every span export fails
``DEADLINE_EXCEEDED`` against ``localhost:4317``, and the platform export block
targets UC tables that do not exist.

``--no-otel`` selects the ``prod-no-otel`` bundle target, which overrides the
three OTel variables. Two things must hold, and both are asserted here from the
bundle file itself rather than from a live workspace:

1. the default ``prod`` target's **resolved** app spec is exactly what `main`
   hardcoded before the variables were introduced — no silent telemetry change
   for existing deploys;
2. ``prod-no-otel`` really turns the tracer off: no ``opentelemetry-instrument``
   entrypoint, no ``OTEL_TRACES_SAMPLER``, and an empty export list.

The flag also only rewrites the *default* target, so pairing it with a custom
``--target`` must warn rather than silently ship OTel-on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"
_BUNDLE_YML = _ROOT / "deploy" / "databricks" / "databricks.yml"

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

# The app spec `main` hardcoded before app_command / app_env /
# otel_export_destinations existed. `prod` must still resolve to exactly this.
_PROD_COMMAND_BEFORE = ["opentelemetry-instrument", "python", "app.py"]
_PROD_ENV_BEFORE = [
    {"name": "AP_LAKEBASE_ENDPOINT", "value_from": "postgres"},
    {"name": "AP_ARTIFACT_VOLUME_PATH", "value_from": "artifact_volume"},
    {"name": "OTEL_TRACES_SAMPLER", "value": "always_on"},
    {"name": "OMNIGENT_FEATURES", "value": "${var.features}"},
]
_PROD_TELEMETRY_BEFORE = [
    {
        "unity_catalog": {
            "logs_table": "${var.otel_table_schema}.otel_logs",
            "metrics_table": "${var.otel_table_schema}.otel_metrics",
            "traces_table": "${var.otel_table_schema}.otel_spans",
        }
    }
]


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    """Load ``deploy.py`` by path — ``deploy/`` is not an installed package."""
    spec = importlib.util.spec_from_file_location("_databricks_deploy_no_otel", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle() -> dict[str, Any]:
    return yaml.safe_load(_BUNDLE_YML.read_text())


def _parse(deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, *extra: str) -> Any:
    monkeypatch.setattr(sys, "argv", ["deploy.py", *_REQUIRED_ARGS, *extra])
    return deploy_mod._parse_args()


def _resolved_app_spec(bundle: dict[str, Any], target: str) -> dict[str, Any]:
    """Resolve one target's app command / env / telemetry from the bundle.

    Mirrors how DAB layers a target's ``variables`` over the top-level variable
    defaults, then substitutes ``${var.<name>}`` in the app resource.

    A top-level declaration carries the value under ``default``; a target
    override assigns the value directly. The direct form is asserted rather than
    accommodated, so a nested declaration smuggled into a target is a failure
    here instead of being silently unwrapped.
    """
    values = {name: spec.get("default") for name, spec in bundle["variables"].items()}
    target_block = bundle["targets"][target]
    for name, value in (target_block.get("variables") or {}).items():
        assert not (
            isinstance(value, dict) and set(value) <= {"default", "type", "description"}
        ), (
            f"target {target!r} overrides {name!r} with a variable *declaration*; "
            "a target override must assign the value directly"
        )
        values[name] = value

    def substitute(node: Any) -> Any:
        if isinstance(node, str):
            name = node.removeprefix("${var.").removesuffix("}")
            if node.startswith("${var.") and node.endswith("}") and name in values:
                return values[name]
            return node
        if isinstance(node, list):
            return [substitute(item) for item in node]
        if isinstance(node, dict):
            return {key: substitute(value) for key, value in node.items()}
        return node

    app = bundle["resources"]["apps"]["omnigent"]
    target_app = ((target_block.get("resources") or {}).get("apps") or {}).get("omnigent") or {}
    return {
        "command": substitute(app["config"]["command"]),
        "env": substitute(app["config"]["env"]),
        "telemetry": substitute(target_app.get("telemetry_export_destinations") or []),
    }


@pytest.mark.parametrize(
    ("target", "expected_command", "expected_env", "expected_telemetry"),
    [
        pytest.param(
            "prod",
            _PROD_COMMAND_BEFORE,
            _PROD_ENV_BEFORE,
            _PROD_TELEMETRY_BEFORE,
            id="prod-unchanged-from-before-the-variables",
        ),
        pytest.param(
            "prod-no-otel",
            ["python", "app.py"],
            [entry for entry in _PROD_ENV_BEFORE if entry["name"] != "OTEL_TRACES_SAMPLER"],
            [],
            id="prod-no-otel-drops-instrumentation-sampler-and-export",
        ),
    ],
)
def test_resolved_app_spec_per_target(
    bundle: dict[str, Any],
    target: str,
    expected_command: list[str],
    expected_env: list[dict[str, str]],
    expected_telemetry: list[Any],
) -> None:
    resolved = _resolved_app_spec(bundle, target)
    assert resolved["command"] == expected_command
    assert resolved["env"] == expected_env
    assert resolved["telemetry"] == expected_telemetry


def test_no_otel_target_overrides_are_direct_values(bundle: dict[str, Any]) -> None:
    """Target overrides must be plain values, the documented DAB shape."""
    overrides = bundle["targets"]["prod-no-otel"]["variables"]
    assert overrides["app_command"] == ["python", "app.py"]
    assert overrides["otel_export_destinations"] == []
    assert isinstance(overrides["app_env"], list)
    assert {entry["name"] for entry in overrides["app_env"]} == {
        "AP_LAKEBASE_ENDPOINT",
        "AP_ARTIFACT_VOLUME_PATH",
        "OMNIGENT_FEATURES",
    }


def test_no_otel_target_has_no_otel_surface_at_all(bundle: dict[str, Any]) -> None:
    """Belt-and-braces: nothing OTel-shaped survives in the resolved spec."""
    resolved = _resolved_app_spec(bundle, "prod-no-otel")
    assert not any("opentelemetry" in part for part in resolved["command"])
    assert not any(entry["name"].startswith("OTEL_") for entry in resolved["env"])
    assert resolved["telemetry"] == []

    # And the default target still exports, so this is an opt-in, not a removal.
    assert _resolved_app_spec(bundle, "prod")["telemetry"] != []


def test_no_otel_shares_prod_workspace_and_state(bundle: dict[str, Any]) -> None:
    """Same host and root_path, so switching targets does not orphan state."""
    prod = bundle["targets"]["prod"]["workspace"]
    no_otel = bundle["targets"]["prod-no-otel"]["workspace"]
    assert prod == no_otel
    assert bundle["targets"]["prod-no-otel"]["mode"] == "production"


def test_no_otel_switches_the_default_target(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parse(deploy_mod, monkeypatch, "--no-otel")
    assert args.target == "prod-no-otel"
    assert "warning" not in capsys.readouterr().out


def test_default_deploy_is_untouched_by_the_new_flag(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without the flag, the target and the bundle vars are exactly as before."""
    args = _parse(deploy_mod, monkeypatch)
    assert args.no_otel is False
    assert args.target == "prod"
    assert "warning" not in capsys.readouterr().out
    # --no-otel is a target selector only; it adds no --var to the CLI call.
    assert not any("app_command" in value for value in deploy_mod._bundle_vars(args))


def test_no_otel_warns_when_paired_with_a_target_that_keeps_otel_on(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An explicit --target wins, so the no-op must be reported, not silent."""
    args = _parse(deploy_mod, monkeypatch, "--no-otel", "--target", "staging")
    assert args.target == "staging"
    out = capsys.readouterr().out
    assert "warning: --no-otel has no effect" in out
    assert "staging" in out
    for name in deploy_mod._OTEL_OFF_VARS:
        assert name in out


def test_no_otel_is_quiet_when_the_explicit_target_already_overrides(
    deploy_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parse(deploy_mod, monkeypatch, "--no-otel", "--target", "prod-no-otel")
    assert args.target == "prod-no-otel"
    assert "warning" not in capsys.readouterr().out


def test_target_override_probe_matches_the_bundle(deploy_mod: ModuleType) -> None:
    """The probe backing the warning must agree with databricks.yml."""
    assert deploy_mod._target_overrides_otel_vars("prod-no-otel") is True
    assert deploy_mod._target_overrides_otel_vars("prod") is False
    assert deploy_mod._target_overrides_otel_vars("no-such-target") is False


def test_target_override_probe_is_none_without_pyyaml(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PyYAML must not turn the warning into a crash or a false claim."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert deploy_mod._target_overrides_otel_vars("prod-no-otel") is None
