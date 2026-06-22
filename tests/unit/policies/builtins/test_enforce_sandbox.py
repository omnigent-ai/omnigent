"""
Tests for the built-in ``enforce_sandbox`` policy
(:mod:`omnigent.policies.builtins.safety`).

Covers:

- Default behaviour (forces ``linux_bwrap``, ``allow_network=True``).
- Custom sandbox type and network override.
- Merge semantics: policy overrides win, agent fields not in the
  override are preserved.
- Agent with no existing sandbox config — created from scratch.
- Non-``__agent_start`` tool calls pass through.
- Non-``tool_call`` events pass through.
- ``write_paths`` / ``read_paths`` / ``env_passthrough`` overrides.
- Unknown fields in the existing sandbox are filtered out.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.builtins.safety import enforce_sandbox
from omnigent.policies.schema import PolicyEvent
from tests.unit.policies.builtins.helpers import tool_call_event as tc


def _agent_start_event(
    agent_name: str = "test-agent",
    harness: str = "claude-sdk",
    sandbox: dict[str, Any] | None = None,
) -> PolicyEvent:
    """Build an ``__agent_start`` tool call event.

    :param agent_name: Agent name, e.g. ``"test-agent"``.
    :param harness: Harness type, e.g. ``"claude-sdk"``.
    :param sandbox: Current sandbox config dict, or ``None``.
    :returns: A ``tool_call`` event dict for ``__agent_start``.
    """
    return tc(
        "sys_agent_start",
        {
            "agent_name": agent_name,
            "harness": harness,
            "sandbox": sandbox,
        },
    )


# ── Default behaviour (linux_bwrap) ──────────────────────────────────────────


def test_enforce_sandbox_default_forces_bwrap() -> None:
    """Default ``enforce_sandbox()`` forces ``linux_bwrap`` with network on.

    If this returns plain ALLOW without data, the sandbox override
    is not being emitted.
    """
    policy = enforce_sandbox()
    result = policy(_agent_start_event(sandbox=None))
    assert result["result"] == "ALLOW"
    assert "data" in result
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["type"] == "linux_bwrap"
    assert sandbox["allow_network"] is True


def test_enforce_sandbox_overrides_existing_type() -> None:
    """When the agent already has ``none``, the policy
    overrides to ``linux_bwrap``.

    If the type is still ``none``, the override is not
    applying.
    """
    policy = enforce_sandbox()
    event = _agent_start_event(sandbox={"type": "none", "allow_network": False})
    result = policy(event)
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["type"] == "linux_bwrap"
    # allow_network should also be overridden to True (the default).
    assert sandbox["allow_network"] is True


# ── Custom sandbox type ──────────────────────────────────────────────────────


def test_enforce_sandbox_custom_type() -> None:
    """Admin can force a different sandbox type.

    If this still returns ``linux_bwrap``, the ``sandbox_type``
    parameter is being ignored.
    """
    policy = enforce_sandbox(sandbox_type="darwin_seatbelt", allow_network=False)
    result = policy(_agent_start_event(sandbox=None))
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["type"] == "darwin_seatbelt"
    assert sandbox["allow_network"] is False


# ── Merge semantics ──────────────────────────────────────────────────────────


def test_enforce_sandbox_preserves_agent_write_paths() -> None:
    """Agent's ``write_paths`` are preserved when policy doesn't override them.

    If ``write_paths`` is missing from the result, the merge is
    replacing instead of merging.
    """
    policy = enforce_sandbox()  # No write_paths override
    event = _agent_start_event(sandbox={"type": "none", "write_paths": ["."]})
    result = policy(event)
    sandbox = result["data"]["arguments"]["sandbox"]
    # Policy overrides type but preserves write_paths.
    assert sandbox["type"] == "linux_bwrap"
    assert sandbox["write_paths"] == ["."]


def test_enforce_sandbox_override_write_paths() -> None:
    """When the policy specifies ``write_paths``, it overrides the agent's.

    If the result still has the agent's original paths, the
    policy override is not winning.
    """
    policy = enforce_sandbox(write_paths=["/tmp"])
    event = _agent_start_event(sandbox={"type": "none", "write_paths": ["."]})
    result = policy(event)
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["write_paths"] == ["/tmp"]


def test_enforce_sandbox_override_read_paths() -> None:
    """Policy ``read_paths`` override works.

    If ``read_paths`` is missing or wrong, the override is broken.
    """
    policy = enforce_sandbox(read_paths=["/data", "/models"])
    result = policy(_agent_start_event(sandbox=None))
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["read_paths"] == ["/data", "/models"]


def test_enforce_sandbox_override_env_passthrough() -> None:
    """Policy ``env_passthrough`` override works.

    If ``env_passthrough`` is missing or wrong, the override is broken.
    """
    policy = enforce_sandbox(env_passthrough=["AWS_PROFILE"])
    result = policy(_agent_start_event(sandbox={"env_passthrough": ["GITHUB_TOKEN"]}))
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["env_passthrough"] == ["AWS_PROFILE"]


# ── No existing sandbox ──────────────────────────────────────────────────────


def test_enforce_sandbox_creates_from_scratch() -> None:
    """When the agent has no sandbox at all (``None``), the policy
    creates one from scratch.

    If ``data`` is missing, the policy is not handling the
    ``sandbox: null`` case.
    """
    policy = enforce_sandbox(sandbox_type="linux_bwrap", allow_network=False)
    event = _agent_start_event(sandbox=None)
    result = policy(event)
    assert result["result"] == "ALLOW"
    sandbox = result["data"]["arguments"]["sandbox"]
    assert sandbox["type"] == "linux_bwrap"
    assert sandbox["allow_network"] is False


# ── Non-__agent_start tools pass through ─────────────────────────────────────


def test_enforce_sandbox_ignores_other_tools() -> None:
    """Tool calls that aren't ``__agent_start`` are allowed unchanged.

    If this returns data, the policy is intercepting unrelated tools.
    """
    policy = enforce_sandbox()
    result = policy(tc("sys_os_shell", {"command": "ls"}))
    assert result["result"] == "ALLOW"
    assert "data" not in result


def test_enforce_sandbox_ignores_non_tool_call_phases() -> None:
    """Non-``tool_call`` events pass through.

    If this returns data, the policy is firing on wrong phases.
    """
    policy = enforce_sandbox()
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "start the agent",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"
    assert "data" not in result


# ── Agent metadata preserved ─────────────────────────────────────────────────


def test_enforce_sandbox_preserves_agent_metadata() -> None:
    """Agent name and harness are preserved in the returned data.

    If these are missing, the policy is clobbering the arguments dict.
    """
    policy = enforce_sandbox()
    result = policy(_agent_start_event(agent_name="my-agent", harness="codex"))
    args = result["data"]["arguments"]
    assert args["agent_name"] == "my-agent"
    assert args["harness"] == "codex"


# ── Unknown sandbox fields filtered ─────────────────────────────────────────


def test_enforce_sandbox_filters_unknown_fields() -> None:
    """Unknown fields in the existing sandbox config are dropped.

    Prevents injection of unsupported fields through the merge.
    If ``rogue_field`` appears in the result, the filter is broken.
    """
    policy = enforce_sandbox()
    event = _agent_start_event(sandbox={"type": "none", "rogue_field": "evil"})
    result = policy(event)
    sandbox = result["data"]["arguments"]["sandbox"]
    assert "rogue_field" not in sandbox
    assert sandbox["type"] == "linux_bwrap"


# ── Edge: empty arguments ────────────────────────────────────────────────────


def test_enforce_sandbox_handles_missing_arguments() -> None:
    """An ``__agent_start`` call with no arguments dict is handled.

    If this crashes, the policy doesn't handle malformed events.
    """
    policy = enforce_sandbox()
    event: PolicyEvent = {
        "type": "tool_call",
        "target": "sys_agent_start",
        "data": {"name": "sys_agent_start"},
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"
    assert result["data"]["arguments"]["sandbox"]["type"] == "linux_bwrap"


# ── Multiple overrides compose correctly ─────────────────────────────────────


@pytest.mark.parametrize(
    "policy_kwargs,expected_sandbox",
    [
        (
            {"sandbox_type": "linux_bwrap", "allow_network": False, "write_paths": ["."]},
            {"type": "linux_bwrap", "allow_network": False, "write_paths": ["."]},
        ),
        (
            {"sandbox_type": "darwin_seatbelt", "read_paths": ["/data"]},
            {"type": "darwin_seatbelt", "allow_network": True, "read_paths": ["/data"]},
        ),
    ],
    ids=["bwrap-no-net-writable-cwd", "seatbelt-with-read-paths"],
)
def test_enforce_sandbox_parametrized(
    policy_kwargs: dict[str, Any],
    expected_sandbox: dict[str, Any],
) -> None:
    """Parametrized: various policy configs produce expected sandbox.

    :param policy_kwargs: Arguments to ``enforce_sandbox()``.
    :param expected_sandbox: Expected sandbox fields in the result.
    """
    policy = enforce_sandbox(**policy_kwargs)
    result = policy(_agent_start_event(sandbox=None))
    sandbox = result["data"]["arguments"]["sandbox"]
    for key, value in expected_sandbox.items():
        assert sandbox[key] == value, f"sandbox[{key!r}] = {sandbox[key]!r}, expected {value!r}"
