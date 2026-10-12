"""Small builders for native-session route tests."""

from typing import Any

from omnigent.spec.types import AgentSpec, ExecutorSpec


def _harness_spec(harness: str, **config: Any) -> AgentSpec:
    """Create an independent minimal agent, keeping harness options at the callsite."""
    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness, **config}),
    )
