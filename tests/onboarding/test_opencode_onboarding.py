"""Tests for opencode entries in the onboarding install/readiness surfaces."""

from omnigent.onboarding.harness_install import (
    OPENCODE_KEY,
    harness_install_spec,
    required_cli_for_harness,
)
from omnigent.onboarding.harness_readiness import (
    configured_harness_map,
    harness_is_configured,
)


def test_install_spec_present():
    spec = harness_install_spec(OPENCODE_KEY)
    assert spec is not None
    assert spec.package == "opencode-ai@~1.17.7"
    assert spec.binary == "opencode"


def test_required_cli_for_opencode():
    spec = required_cli_for_harness("opencode")
    assert spec is not None and spec.binary == "opencode"


def test_configured_map_includes_opencode():
    assert OPENCODE_KEY in configured_harness_map()


def test_harness_is_configured_resolvable():
    # Must return a bool without raising (value depends on whether the
    # opencode binary is installed in the test env).
    assert isinstance(harness_is_configured("opencode"), bool)
