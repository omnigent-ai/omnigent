"""Function-policy validation is consistent across uploaded bundle formats."""

from __future__ import annotations

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.bundles import validate_agent_bundle
from tests.server.test_bundles import _make_bundle_bytes

_SHIM_PATH = "omnigent.spec._omnigent_legacy_shim.build"
_REGISTERED = "omnigent.policies.builtins.safety.max_tool_calls_per_session"
_CUSTOM = "example_policies.review"


def _bundle(policy: dict[str, object], shape: str) -> bytes:
    config: dict[str, object] = {"name": "review_agent", "prompt": "hello"}
    if shape == "single_file":
        config["executor"] = {"harness": "claude-sdk"}
        config["policies"] = {"review": {"type": "function", **policy}}
        path = "agent.yaml"
    else:
        config["spec_version"] = 1
        config["executor"] = {"type": "omnigent", "config": {"harness": "claude-sdk"}}
        config["guardrails"] = {"policies": {"review": {"type": "function", **policy}}}
        path = "config.yaml"
    return _make_bundle_bytes({path: yaml.safe_dump(config)})


@pytest.mark.parametrize("shape", ["single_file", "directory"])
@pytest.mark.parametrize(
    "function",
    [
        _CUSTOM,
        {"path": _CUSTOM, "arguments": {}},
        {"path": _SHIM_PATH, "arguments": {"target": _CUSTOM}},
    ],
    ids=["string", "factory", "wrapped"],
)
def test_uploaded_function_requires_registered_handler(shape: str, function: object) -> None:
    with pytest.raises(OmnigentError, match="not a registered policy handler") as raised:
        validate_agent_bundle(_bundle({"function": function}, shape))
    assert raised.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("shape", ["single_file", "directory"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_uploaded_function_accepts_registered_factory(shape: str, wrapped: bool) -> None:
    function = (
        {"path": _SHIM_PATH, "arguments": {"target": _REGISTERED, "factory_kwargs": {"limit": 5}}}
        if wrapped
        else {"path": _REGISTERED, "arguments": {"limit": 5}}
    )
    spec = validate_agent_bundle(_bundle({"function": function}, shape))
    assert spec.name == "review_agent"


@pytest.mark.parametrize("key", ["handler", "callable"])
def test_uploaded_legacy_handler_accepts_registered_factory(key: str) -> None:
    spec = validate_agent_bundle(
        _bundle({key: _REGISTERED, "factory_params": {"limit": 5}}, "single_file")
    )
    assert spec.name == "review_agent"


@pytest.mark.parametrize("shape", ["single_file", "directory"])
def test_trusted_function_can_use_custom_handler(shape: str) -> None:
    spec = validate_agent_bundle(
        _bundle({"function": {"path": _CUSTOM}}, shape), enforce_handler_allowlist=False
    )
    assert spec.name == "review_agent"
