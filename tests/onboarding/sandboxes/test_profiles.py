"""Admin profiles accept only resource overrides and native egress policy fields."""

import click
import pytest

from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import AgentSandboxWarmPoolLauncher
from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import validate_profile_config


def test_profile_normalization_preserves_network_intent() -> None:
    assert validate_profile_config({}) == {}
    assert validate_profile_config({"egress": []}) == {"egress": []}
    result = validate_profile_config(
        {
            "image": " host:v1 ",
            "resources": {"requests": {"cpu": " 500m "}},
            "egress": [{"ports": [{"port": 443}], "to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}]}],
        }
    )
    assert result["image"] == "host:v1"
    assert result["resources"] == {"requests": {"cpu": "500m"}}
    assert result["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]
    assert validate_profile_config(result) == result


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"image": ""},
        {"image": None},
        {"secret_name": "credentials"},
        {"resources": {"cpu": "1"}},
        {"resources": {"requests": {"cpu": 2}}},
        {"resources": {"limits": {"memory": "-1Gi"}}},
        {"resources": {"limits": {"memory": "2watts"}}},
        {"egress": None},
        {"egress": {}},
        {"egress": [{"from": []}]},
        {"egress": [{"ports": [{"port": True}]}]},
        {"egress": [{"ports": [{"port": 0}]}]},
        {"egress": [{"ports": [{"endPort": 443}]}]},
        {"egress": [{"ports": [{"port": 443, "endPort": 80}]}]},
        {"egress": [{"to": [{"ipBlock": {"cidr": "example.com"}}]}]},
        {"egress": [{"to": [{"ipBlock": {"cidr": "192.0.2.0/24", "except": ["::/0"]}}]}]},
        {"egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}, "podSelector": {}}]}]},
    ],
)
def test_profile_rejects_invalid_config(config: object) -> None:
    with pytest.raises(ValueError):
        validate_profile_config(config)


def test_profile_merges_resources_without_losing_deployment_defaults() -> None:
    launcher = KubernetesSandboxLauncher(
        image="original:v1", resources={"requests": {"cpu": "1", "memory": "4Gi"}}
    )
    launcher.apply_profile({"image": "profile:v1", "resources": {"requests": {"cpu": "2"}}})
    assert launcher._resolve_image() == "profile:v1"
    assert launcher._resources == {"requests": {"cpu": "2", "memory": "4Gi"}, "limits": {}}


def test_selected_profiles_reject_unsupported_providers_even_when_empty() -> None:
    class UnsupportedLauncher(SandboxHostLauncher):
        provider = "unsupported"

        def prepare(self):
            pass

        def provision(self, name):
            return name

        def start_host(self, *args, **kwargs):
            return ""

    with pytest.raises(click.ClickException, match="not supported"):
        UnsupportedLauncher().apply_profile({})
    with pytest.raises(click.ClickException, match="warm pools"):
        AgentSandboxWarmPoolLauncher(warm_pool="ready").apply_profile({})
    direct = AgentSandboxWarmPoolLauncher()
    direct.apply_profile({})
    with pytest.raises(click.ClickException, match="warm-pool allocations"):
        direct._allocation(None)
