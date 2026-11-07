"""Constructor policy controls independent of SDK authentication."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_POLICIES = {
    "core": "omnigent/util/databricks_sdk.py",
    "deployment": "deploy/databricks/src/databricks_sdk_policy.py",
    "triage": ".github/triage_v2/src/issue_prioritization/databricks_sdk_policy.py",
}


@pytest.fixture(params=_POLICIES)
def policy(request):
    path = Path(__file__).resolve().parents[2] / _POLICIES[request.param]
    spec = importlib.util.spec_from_file_location(f"sdk_policy_{request.param}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


@pytest.mark.parametrize("kwargs", [{}, {"profile": None}, {"profile": "owned"}])
def test_workspace_client_legacy_keeps_exact_kwargs(monkeypatch, kwargs, policy):
    config_calls = []
    client_calls = []
    omitted = object()

    class LegacyConfig:
        def __init__(self, *, profile=None):
            config_calls.append(profile)

    def client(*, profile=omitted):
        client_calls.append(profile)
        return "synthetic-client"

    monkeypatch.setattr("databricks.sdk.config.Config", LegacyConfig)

    assert policy[0].workspace_client_with_cli_cache(client, **kwargs) == "synthetic-client"
    assert client_calls == [kwargs.get("profile", omitted)]
    assert config_calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"profile": None},
        {"profile": "owned"},
        {"profile": "owned", "product": "custom", "product_version": "9"},
    ],
)
def test_workspace_client_uses_public_config_and_preserves_product_defaults(
    monkeypatch, kwargs, policy
):
    calls = []

    class Config:
        databricks_cli_force_refresh = None

        def __init__(
            self, *, databricks_cli_force_refresh, product, product_version, profile=None
        ):
            calls.append((databricks_cli_force_refresh, product, product_version, profile))

    class Client:
        def __init__(
            self, *, config=None, profile=None, product="synthetic-client", product_version="7.2"
        ):
            assert profile is None
            self.config = config

    monkeypatch.setattr("databricks.sdk.config.Config", Config)

    result = policy[0].workspace_client_with_cli_cache(Client, **kwargs)

    assert isinstance(result.config, Config)
    assert calls == [
        (
            False,
            kwargs.get("product", "synthetic-client"),
            kwargs.get("product_version", "7.2"),
            kwargs.get("profile"),
        )
    ]


def test_missing_config_probe_preserves_legacy_client(monkeypatch, policy):
    calls = []

    def client(*, profile):
        calls.append(profile)
        return "synthetic-client"

    monkeypatch.setitem(sys.modules, "databricks.sdk.config", None)

    assert policy[0].workspace_client_with_cli_cache(client, profile="owned") == "synthetic-client"
    assert calls == ["owned"]


def test_policy_import_keeps_sdk_and_executor_unloaded(policy):
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('sdk_policy', {str(policy[1])!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert 'databricks.sdk' not in sys.modules; "
        "assert 'omnigent' not in sys.modules; "
        "assert 'omnigent.inner.databricks_executor' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("directory", "module"),
    [
        ("deploy/databricks", "src.databricks_sdk_policy"),
        (".github/triage_v2/src", "issue_prioritization.databricks_sdk_policy"),
    ],
)
def test_standalone_package_import_has_no_omnigent_dependency(directory, module):
    directory = Path(__file__).resolve().parents[2] / directory
    code = (
        "import importlib,sys; "
        f"sys.path.insert(0, {str(directory)!r}); importlib.import_module({module!r}); "
        "assert 'databricks.sdk' not in sys.modules; "
        "assert 'omnigent' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)
