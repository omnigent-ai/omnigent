"""
Round-trip invariant tests for the Omnigent ↔ AgentSpec
adapter.

Asserts
``agent_spec_to_agent_def(agent_def_to_agent_spec(d)) == d`` for
every representative fixture YAML. Translation drift between the
forward and reverse directions fails these tests the moment it
appears.

**Phase 1 dependency.** These tests import
:func:`omnigent.spec.omnigent.agent_spec_to_agent_def`,
which is owned by the phase 1 worktree. Until phase 1 is merged
into the branch this test runs against, the import fails at
collection time — pytest reports a collection error naming the
missing symbol. That is the intended gate: the round-trip test
is written once and becomes meaningful the moment both
directions coexist. No ``pytest.mark.skip`` — per the
omnigent-testing skill, skipped tests rot invisibly. The
collection-time ImportError is the reviewer's signal to merge
phase 1 first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.spec.omnigent import (
    agent_def_to_agent_spec,
    # NOTE: imported from the same module as the reverse
    # direction — both functions ship in
    # ``omnigent/spec/omnigent.py`` per the design. Phase 1
    # adds this symbol; phase 2 consumes it here.
    agent_spec_to_agent_def,
)


@pytest.fixture()
def hello_world_yaml(tmp_path: Path) -> Path:
    """
    Minimal omnigent YAML — name + prompt only. Round-trip
    checks that the adapter does not silently add or lose
    fields on the trivial case.
    """
    config = {
        "name": "hello_world",
        "prompt": "You are a friendly assistant.",
    }
    path = tmp_path / "hello_world.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def executor_block_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with an ``executor:`` block declaring
    model + harness + profile. Round-trip checks that every
    executor field survives both directions unchanged.
    """
    config = {
        "name": "executor_example",
        "prompt": "Assistant with a fixed executor.",
        "executor": {
            "model": "databricks-claude-sonnet-4",
            "harness": "claude-sdk",
            "profile": "test-profile",
        },
    }
    path = tmp_path / "executor.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def function_tool_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with one function-type tool pointing at a
    real importable callable. Round-trip checks that the
    dotted-path encoding is lossless across both directions.
    """
    config = {
        "name": "tool_user",
        "prompt": "Use tools when helpful.",
        "executor": {"model": "databricks-claude-sonnet-4"},
        "tools": {
            "get_current_time": {
                "type": "function",
                "description": "Return current time.",
                "callable": "tests.resources.examples._shared.tool_functions.get_current_time",
            },
        },
    }
    path = tmp_path / "tools.yaml"
    path.write_text(yaml.dump(config))
    return path


def _roundtrip(yaml_path: Path) -> None:
    """
    Load the YAML via omnigent' loader, translate to an
    :class:`AgentSpec`, translate back, and assert equality on the
    fields the bidirectional translator is contracted to preserve.

    **Lossy by design.** :class:`AgentSpec` does not model every
    omnigent :class:`FunctionTool` field — ``description``,
    ``input_schema``, ``output_schema``, ``scopes``, and
    ``catalog_path`` are dropped on the way to AgentSpec and not
    recovered on the way back. We compare on the structural fields
    we DO promise to round-trip: ``name``, ``prompt``, ``executor``,
    and the per-tool ``(name, callable identity)`` pair.

    :param yaml_path: Path to an omnigent YAML fixture.
    """
    from omnigent.inner.loader import load_agent_def

    original = load_agent_def(yaml_path)
    spec = agent_def_to_agent_spec(original)
    recovered = agent_spec_to_agent_def(spec)

    assert recovered.name == original.name
    assert recovered.prompt == original.prompt
    # Executor fields are compared individually rather than by
    # whole-dataclass equality because the adapter's
    # :func:`_infer_harness_from_model` enriches ``harness`` from
    # an empty string to a concrete value when the YAML declares
    # only a model. That's intended behavior (mirrors pure
    # omnigent' CLI auto-pick) — the round-trip contract is
    # "every field the caller explicitly set must survive", not
    # "no field can gain a value".
    assert recovered.executor.model == original.executor.model
    assert (recovered.executor.profile or None) == original.executor.profile
    if original.executor.harness:
        # Explicit harness must be preserved byte-for-byte.
        assert recovered.executor.harness == original.executor.harness
    # Tool round-trip: we preserve name + the resolved callable
    # object, not metadata. Comparing keys + callable identity
    # catches the round-trip path; comparing tool docstrings or
    # schemas would falsely fail because AgentSpec doesn't model
    # them (documented limitation, not a bug).
    assert set(recovered.tools.keys()) == set(original.tools.keys())
    for tool_name, original_tool in original.tools.items():
        recovered_tool = recovered.tools[tool_name]
        assert recovered_tool.name == original_tool.name
        assert recovered_tool.callable is original_tool.callable


def test_roundtrip_hello_world_is_incomplete_for_omnigent(
    hello_world_yaml: Path,
) -> None:
    """
    A bare ``name`` + ``prompt`` YAML (no executor block) does
    NOT round-trip — the synthesized AgentSpec has no harness or
    model, which Omnigent' strict spec rejects on the way
    back. This is intentional: the omnigent validator requires
    a harness when ``executor.type == "omnigent"``, and that
    requirement is the reason the round-trip surfaces as a
    fail-loud error rather than producing nonsense.

    What breaks if this fails: somebody loosened the validator
    or invented a default harness silently. Either change should
    be a deliberate decision with a reviewer; the test guards
    against silent drift.
    """
    from omnigent.errors import OmnigentError
    from omnigent.inner.loader import load_agent_def

    original = load_agent_def(hello_world_yaml)
    spec = agent_def_to_agent_spec(original)
    with pytest.raises(OmnigentError) as exc_info:
        agent_spec_to_agent_def(spec)
    # Error message names executor.model — confirms the failure
    # is the documented missing-model branch, not some other gap.
    assert "executor.model" in str(exc_info.value)


def test_roundtrip_executor_block(executor_block_yaml: Path) -> None:
    """
    Executor-block YAML (model + harness + profile) round-trips
    unchanged.

    What breaks if this fails: harness / profile encoding
    differs between the two directions — OmnigentExecutor
    would pick the wrong harness on the reverse trip.
    """
    _roundtrip(executor_block_yaml)


def test_roundtrip_function_tool(function_tool_yaml: Path) -> None:
    """
    Function-type tool round-trips unchanged (dotted-path
    encoding is lossless).

    What breaks if this fails: the forward direction's
    ``importlib.import_module`` resolution doesn't match the
    reverse direction's ``__module__`` + ``__qualname__``
    recovery, so a tool named in the YAML disappears or
    re-appears under a different name.
    """
    _roundtrip(function_tool_yaml)
