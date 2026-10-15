"""Structural test for the Databricks Genie example (examples/genie).

Genie is a single-agent recipe that registers a remote Databricks AI/BI Genie
space as the agent itself, over the ``databricks-genie`` harness. Pure
spec-load — no LLM, no credentials, no live workspace. The live gate for the
harness is ``test_per_harness_databricks_genie.py`` (which drives a real Genie
space via CLI overrides); this file pins the shipped bundle's spec shape and
the exact fields ``_build_databricks_genie_spawn_env`` consumes from it.

What breaks if this fails:
- the bundle stops parsing under a spec-schema change (users following the
  README quick start get a parse error),
- the harness id, space-id placeholder, or auth block drifts away from what
  the docs tell users to edit,
- the spawn-env builder stops resolving the bundle's fields into the
  ``HARNESS_DATABRICKS_GENIE_*`` env vars the harness wrap reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_databricks_genie_spawn_env
from omnigent.spec import load
from omnigent.spec.types import AgentSpec, DatabricksAuth

# tests/e2e/omnigent/test_example_genie.py -> repo root is 3 parents up.
_GENIE_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "genie"

_SPACE_ID_PLACEHOLDER = "REPLACE_WITH_GENIE_SPACE_ID"


@pytest.fixture(scope="module")
def genie_spec() -> AgentSpec:
    """Load and validate the genie bundle once for the module."""
    return load(_GENIE_BUNDLE, expand_env=False)


def test_genie_name_harness_and_space_id(genie_spec: AgentSpec) -> None:
    """
    The agent runs on the databricks-genie harness with the space id carried in
    ``executor.model`` — the placeholder the README quick start tells users to
    replace with their Genie room's id.
    """
    assert genie_spec.name == "sales_genie"
    assert genie_spec.executor.config.get("harness") == "databricks-genie"
    assert genie_spec.executor.model == _SPACE_ID_PLACEHOLDER


def test_genie_auth_is_a_databricks_profile(genie_spec: AgentSpec) -> None:
    """Auth is a Databricks CLI profile — the only credential path Genie has."""
    assert genie_spec.executor.auth == DatabricksAuth(profile="DEFAULT")


def test_genie_bundle_resolves_through_the_spawn_env_builder(
    genie_spec: AgentSpec,
) -> None:
    """The bundle's fields land in the env vars the harness wrap reads."""
    env = _build_databricks_genie_spawn_env(genie_spec)
    assert env["HARNESS_DATABRICKS_GENIE_MODEL"] == _SPACE_ID_PLACEHOLDER
    assert env["HARNESS_DATABRICKS_GENIE_PROFILE"] == "DEFAULT"
