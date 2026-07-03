"""Structural test for ``examples/agent_with_uc_tools.yaml``.

The example registers Databricks Unity Catalog functions as tools
via ``catalog_path:`` entries (``ai_query`` and a dotted
three-level ``my_catalog.my_schema.classify_sentiment``). Executing
those tools end-to-end needs a live workspace, a running SQL
warehouse (``DATABRICKS_WAREHOUSE_ID``), and the named UC functions
actually existing in that workspace's catalog — none of which the
e2e shard (or a developer laptop) has. The previous one-shot run
under the ``ai-oss`` gateway profile timed out on the slow model
and could never satisfy the ``profile: oss`` auth block the YAML
hardcodes; it was suppressed under ``model-gateway-compat``.

This test instead exercises the part of the example we can
guard without that infra: the spec parser + ``AgentDef``
translation for ``catalog_path:`` tool entries and the
``executor`` harness/model resolution. UC tool *parameters* are
author-supplied in the YAML and are NOT resolved against a
workspace at registration time (see
``omnigent/runner/uc_function.py`` — metadata fetch at agent-build
time is called out there as a future enhancement), so loading the
def is a faithful, infra-free check of everything the spec layer
owns.

**What breaks if this fails:**
- Spec parser regresses on ``catalog_path:`` tool entries (bare
  identifier ``ai_query`` and dotted ``a.b.c`` forms).
- ``FunctionTool.catalog_path`` stops being populated from the
  YAML, so UC tools silently lose their function reference.
- ``executor`` harness/model resolution regresses for the
  ``openai-agents`` + ``databricks-gpt-*`` pairing the example
  pins.

A live invocation path stays covered by the unit tests in
``tests/runner/`` that exercise ``uc_function`` against a stubbed
``WorkspaceClient``.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._example_helpers import validate_agent_def_structure


def test_agent_with_uc_tools_structure(
    omnigent_python: Path,
    omnigent_repo_root: Path,
) -> None:
    """
    Parse + translate ``agent_with_uc_tools.yaml`` and assert the
    resulting :class:`AgentDef` carries both UC-backed tools and
    the pinned ``openai-agents`` executor harness.

    Infra-free (no gateway, no SQL warehouse): the load snippet
    runs in the venv interpreter and asserts on the translated
    def, the same way other can't-run-on-a-laptop examples are
    guarded via :func:`validate_agent_def_structure`.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Repo root for subprocess cwd so
        the example's dotted module paths resolve.
    """
    validate_agent_def_structure(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        example_name="agent_with_uc_tools",
        expected_name="uc_tool_agent",
        expected_tools={"ask_llm", "classify_text"},
        expected_executor_harness="openai-agents",
    )
