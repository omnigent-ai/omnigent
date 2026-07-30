"""
Tests for validator-layer handling of the guardrails block.

Phase 0 scope: the parser (see ``test_policy_parser.py``) does
all the §13 spec-load rejections loudly. This file covers the
small remaining surface — that ``validate()`` accepts a
well-formed ``AgentSpec`` with guardrails attached, and
doesn't regress existing validation when the new field is
absent.

Runtime-layer cross-field checks (``function.path``
resolvability, label key cross-references) are deferred to
the phase that owns the runtime object — Phase 4 for
FunctionPolicy path resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec.parser import parse
from omnigent.spec.validator import validate


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """Minimal agent dir shared across validator tests."""
    (tmp_path / "config.yaml").write_text("")
    return tmp_path


def _write_config(agent_dir: Path, config_yaml: str) -> Path:
    """Overwrite the fixture's config.yaml with test-specific content."""
    (agent_dir / "config.yaml").write_text(config_yaml)
    return agent_dir


def test_validate_passes_without_guardrails(agent_dir: Path) -> None:
    """Existing validator path still green when `guardrails:`
    is absent — regression guard for the AgentSpec extension."""
    _write_config(
        agent_dir,
        """
spec_version: 1
name: no-guardrails
executor:
  config:
    harness: claude-sdk
""",
    )
    result = validate(parse(agent_dir))
    # Empty errors list means valid; no assertion-depth
    # concerns here because ``.errors`` IS the value under
    # test — not a proxy for it.
    assert result.errors == []
    assert result.valid is True


def test_validate_passes_with_full_guardrails(agent_dir: Path) -> None:
    """Full guardrails block with labels + all three policy
    types parses AND validates cleanly."""
    _write_config(
        agent_dir,
        """
spec_version: 1
name: full-guardrails
executor:
  config:
    harness: claude-sdk
guardrails:
  labels:
    integrity:
      initial: "1"
      values: ["0", "1"]
  policies:
    taint_web:
      type: function
      on: [tool_call:web_search]
      function:
        path: omnigent.policies.function.make_fixed_action_callable
        arguments:
          action: allow
          set_labels:
            integrity: "0"
      set_labels: [integrity]
    block_canada:
      type: function
      function:
        path: omnigent.policies.builtins.prompt.prompt_policy
        arguments:
          prompt: Deny if user mentions Canada.
    rate_limit:
      type: function
      on: [tool_call]
      function:
        path: myorg.policies.rate_limit
        arguments: {limit: 10}
  ask_timeout: 30
""",
    )
    spec = parse(agent_dir)
    # Sanity: the parse produced the guardrails we expect —
    # if this assertion fails, the validator failure below
    # would be hiding a parser bug.
    assert spec.guardrails is not None
    assert len(spec.guardrails.policies) == 3

    result = validate(spec)
    # If this breaks, it means `validate()` grew a new check
    # that rejects a shape the parser accepts — investigate
    # which rule, and decide whether to reject earlier (in
    # the parser) or relax the validator.
    assert result.errors == [], (
        f"Expected validate() to pass on a spec that parsed cleanly. Errors: {result.errors}"
    )


def test_validate_passes_with_empty_guardrails_block(agent_dir: Path) -> None:
    """``guardrails: {}`` → validator still green. The block
    is allowed to be empty (no labels / no policies) —
    agents may opt into guardrails incrementally."""
    _write_config(
        agent_dir,
        """
spec_version: 1
name: empty-guardrails
executor:
  config:
    harness: claude-sdk
guardrails: {}
""",
    )
    spec = parse(agent_dir)
    assert spec.guardrails is not None
    # ask_timeout defaulted, labels/policies absent.
    assert spec.guardrails.labels is None
    assert spec.guardrails.policies is None

    result = validate(spec)
    assert result.valid is True


def test_validate_does_not_create_errors_on_policy_names(agent_dir: Path) -> None:
    """Policy names come from YAML keys — YAML parsing already
    dedupes silently. Validator should not raise new errors
    tied to names; if this test starts failing, someone added
    a names-related validator rule without updating the
    parser to reject duplicates first."""
    _write_config(
        agent_dir,
        """
spec_version: 1
name: named-policies
executor:
  config:
    harness: claude-sdk
guardrails:
  policies:
    policy_a:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
    policy_b:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
""",
    )
    result = validate(parse(agent_dir))
    assert result.errors == []


@pytest.mark.parametrize(
    "reserved_name",
    [
        # A framework-owned name still reserved post-elicitation-refactor.
        "web_fetch",
        # Reserved by the model catalog: intercepted by name in the
        # runner's tool dispatch, so a user tool shadowing it would never
        # be invoked (its calls would silently route to the builtin).
        "sys_list_models",
    ],
)
def test_validate_rejects_local_tool_colliding_with_builtin(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    """A user-authored local tool cannot use a name that
    collides with a reserved builtin (POLICIES.md §15.8).

    (``request_approval`` is no longer reserved: policy ASKs
    surface as MCP-shape elicitations, not synthetic
    function_calls, so the carve-out went away. See the
    dedicated regression test below.)
    """
    (tmp_path / "config.yaml").write_text(
        """
spec_version: 1
name: shadows-builtin
executor:
  config:
    harness: claude-sdk
""",
    )
    # Create a local tool file that would be discovered under the
    # reserved name. The parser picks the tool name from the
    # filename stem, so a file ``<reserved_name>.py`` tries to
    # register a tool under the reserved name.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "python").mkdir()
    (tmp_path / "tools" / "python" / f"{reserved_name}.py").write_text(
        "def handler(args): return 'hi'\n",
    )
    spec = parse(tmp_path)
    result = validate(spec)
    assert not result.valid
    # Exactly ONE collision error — not "at least one", which
    # would let a runaway rule firing N times pass silently.
    # If more errors appear, either another rule is also
    # flagging or our check is too eager.
    matching = [e for e in result.errors if reserved_name in e.message and "reserved" in e.message]
    assert len(matching) == 1, (
        f"Expected exactly 1 reserved-name collision error for "
        f"{reserved_name}; got {len(matching)}: {matching}"
    )


def test_validate_accepts_local_tool_named_request_approval(
    tmp_path: Path,
) -> None:
    """``request_approval`` is no longer a reserved name.

    Pre-refactor, policy ASKs surfaced as a synthetic
    ``request_approval`` function_call so the name was reserved
    to prevent user tools from shadowing the framework's
    emission. Under the elicitation refactor, ASKs surface as
    ``response.elicitation_request`` SSE events with a
    distinct ``elicitation_id`` correlation key — no
    function_call, no reserved name. User specs are now free
    to declare a tool called ``request_approval``.

    This test is a regression guard: re-introducing the
    reservation (e.g. by re-adding the row to
    ``_BUILTIN_REGISTRY``) would silently re-break user specs
    that took advantage of the freed name.
    """
    (tmp_path / "config.yaml").write_text(
        """
spec_version: 1
name: uses-the-freed-name
executor:
  config:
    harness: claude-sdk
""",
    )
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "python").mkdir()
    (tmp_path / "tools" / "python" / "request_approval.py").write_text(
        "def handler(args): return 'hi'\n",
    )
    spec = parse(tmp_path)
    result = validate(spec)
    # No reserved-name collision should fire.
    reserved_errors = [
        e for e in result.errors if "request_approval" in e.message and "reserved" in e.message
    ]
    assert reserved_errors == [], (
        f"Expected no reserved-name collision for request_approval "
        f"(name was freed by the elicitation refactor); got: {reserved_errors}"
    )


def test_validate_accepts_non_colliding_local_tool(
    tmp_path: Path,
) -> None:
    """Control for the above — a uniquely-named local tool
    passes. Without this, a bug that flagged every local
    tool as a collision would be undetectable from the
    positive test alone."""
    (tmp_path / "config.yaml").write_text(
        """
spec_version: 1
name: safe-local-tool
executor:
  config:
    harness: claude-sdk
""",
    )
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "python").mkdir()
    (tmp_path / "tools" / "python" / "my_custom_tool.py").write_text(
        "def handler(args): return 'hi'\n",
    )
    spec = parse(tmp_path)
    result = validate(spec)
    assert result.errors == []
