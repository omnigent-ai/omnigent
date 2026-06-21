"""Drift guard: every agent — dir-shaped ``examples/<name>/`` or
``tests/resources/examples/<name>/`` (containing ``config.yaml``),
single-YAML ``examples/<name>.yaml`` or
``tests/resources/examples/<name>.yaml``, or test-only
``tests/resources/agents/<name>/`` — must have a dedicated
``test_example_<name>.py`` file under ``tests/e2e/omnigent/``.

The set of agent roots scanned here is kept in lock-step with the
resolution order in
``tests/e2e/omnigent/_example_helpers.py::example_yaml_path`` — the
helper the per-example tests use to find their YAML. If the guard
scans fewer roots than the helper resolves, a ``test_example_*.py``
that points at a real agent in the un-scanned root looks "orphaned"
to the guard even though it runs fine. (That exact skew —
``tests/resources/examples/`` being resolvable by the helper but
invisible to the guard — is what this file historically tripped on.)

When a new agent lands in any of those roots, the author should
add a test file in the same commit. This test fails loud if an
agent ships without one, and loud again if a test file points
at an agent that no longer exists.

Not a functional test — just a structural cross-check so the
coverage-per-agent rule can't silently drift.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _is_agent_yaml(path: Path) -> bool:
    """
    Whether a top-level ``.yaml`` is an agent spec (has a ``name:``)
    rather than a non-agent config that merely lives alongside the
    examples — e.g. ``server_config_with_policies.yaml``, which is a
    ``omnigent server --config`` file (only ``policies:``), not an
    agent. Mirrors the ``missing required key 'name'`` check the spec
    loader itself uses to reject non-agent YAMLs.

    :param path: Candidate ``.yaml`` / ``.yml`` file.
    :returns: ``True`` when the parsed mapping has a ``name`` key.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and "name" in data


def _scan_agent_root(root: Path, *, require_config_yaml: bool) -> set[str]:
    """
    Collect agent identities under one root: dir-shaped agents
    (directory name) and single-YAML demos (filename stem).

    :param root: Directory to scan (skipped if it does not exist).
    :param require_config_yaml: When ``True``, a directory counts
        only if it contains ``config.yaml`` (AGENTSPEC bundles —
        excludes non-agent dirs like ``examples/databricks_apps/``).
        When ``False``, any directory counts (test-only fixtures
        under ``tests/resources/agents/`` may be single-file
        bundles).
    :returns: Set of agent names found under *root*.
    """
    found: set[str] = set()
    if not root.is_dir():
        return found
    for p in root.iterdir():
        if p.name.startswith(("_", ".")):
            continue
        if p.is_dir():
            if not require_config_yaml or (p / "config.yaml").is_file():
                found.add(p.name)
        elif p.is_file() and p.suffix in {".yaml", ".yml"} and _is_agent_yaml(p):
            # Single-YAML demo: filename stem is the agent identity.
            found.add(p.stem)
    return found


# Agents that have e2e tests in files other than the
# ``test_example_<name>.py`` naming convention (historical, pre-
# unification). Kept as an explicit allow-list so *new* agents
# can't slip past this guard by accident — expanding the list
# requires editing it here.
_ALT_COVERED: frozenset[str] = frozenset(
    {
        # Covered by test_yaml_hello_world.py (via agent_with_tools
        # fixture) and many dedicated hello_world-named e2e tests
        # under tests/e2e/omnigent/test_run_omnigent_* etc.
        "hello_world",
        # Covered by test_yaml_hello_world.py's tool-dispatch test.
        "agent_with_tools",
        # Covered by test_yaml_policies.py.
        "agent_with_policies",
        # Covered by tests/e2e/test_coder_subagent.py +
        # tests/e2e/test_chat_e2e.py.
        "coder",
        # Covered by tests/e2e/omnigent/test_run_omnigent_coding_supervisor.py
        # (seven test functions).
        "coding_supervisor",
        # Covered by tests/e2e/test_openai_coder_*.py.
        "openai-coder",
        # Covered by tests/e2e/omnigent/test_repl_overview_terminal_visibility.py.
        "terminal_workers",
        # Pre-existing coverage gaps — ``chat_model`` is exercised
        # by ``web/``'s integration flow (``web/README.md`` leads
        # the dev-server README with it) and ``coding_supervisor_openai``
        # is the OpenAI-model sibling of ``coding_supervisor`` that
        # reuses the same sub-agent coverage. Both are allowlisted
        # rather than split into dedicated e2e files because no
        # behavior they exercise is unique.
        "chat_model",
        "coding_supervisor_openai",
        # Test-only fixtures under tests/resources/agents/ that
        # don't have test_example_<name>.py files — these are
        # loaded ad-hoc by specific test files (e.g.
        # claude-coder, coding-supervisor, terminal_supervisor are
        # loaded by tests/e2e/conftest.py fixtures; ask-demo,
        # compaction-test, terminal_test are referenced by
        # name from existing e2e tests).
        "ask-demo",
        "claude-coder",
        "coding-supervisor",
        "compaction-test",
        # Test-only fixtures added with OMNIGENT_TERMINAL_BRIDGE (commits
        # 3d9dd0a / 1f9a3a8). Loaded by:
        # - sys-terminal-test → tests/e2e/test_sys_terminal_e2e.py
        #   via the sys_terminal_test_agent fixture in
        #   tests/e2e/conftest.py.
        # - supervisor-terminal-test → tests/e2e/test_repl_terminal_overview_e2e.py
        #   for the parametrized parent+sub-agent terminal sidebar
        #   test.
        "supervisor-terminal-test",
        "sys-terminal-test",
        # Skills-filter test fixtures under tests/resources/agents/.
        # Loaded by tests/e2e/test_codex_skills_filter_e2e.py,
        # test_pi_skills_filter_e2e.py, and
        # test_claude_coder_skills.py.
        "codex_skills_all",
        "codex_skills_list",
        "codex_skills_none",
        "pi_skills_all",
        "pi_skills_list",
        "pi_skills_none",
        "skills_all",
        "skills_list",
        "skills_none",
        # inbox_test is loaded by test_sys_async_inbox_e2e.py /
        # test_sys_async_inbox_harness_e2e.py.
        "inbox_test",
        # timer-test is loaded by test fixtures for sys_timer_*
        # tool tests.
        "timer-test",
        # ralph_loop is a loop-mode demo; no dedicated e2e yet.
        "ralph_loop",
        # ── tests/resources/examples/ agents covered by name elsewhere ──
        # agent_with_client_tools: client-tool knobs are asserted in
        # tests/spec/test_tool_runtime.py (loads the YAML directly).
        "agent_with_client_tools",
        # risk_score_agent: the built-in session-risk-score policy is
        # exercised in tests/runtime/policies/test_example_omnigent_yamls.py.
        "risk_score_agent",
        # ── tests/resources/agents/ fixtures covered by name elsewhere ──
        # workspace-file-writer: loaded by the changed-files e2e tests
        # (test_filesystem_changed_files_e2e.py /
        # test_non_git_changed_files_e2e.py).
        "workspace-file-writer",
        # sdk-chat-builtin: single-YAML fixture loaded by name as the
        # fork-switch target in the native→SDK e2e tests
        # (test_host_claude_native_fork_e2e.py, test_switch_agent_e2e.py,
        # test_switch_agent_native_e2e.py, test_sessions_fork_e2e.py).
        "sdk-chat-builtin",
        "sandbox-deps-os-env",
    }
)

# ``archer`` is retained under tests/resources/examples only as a
# shared uploaded-agent fixture for legacy e2e tests. It is no longer
# a shipped/example agent and its dedicated Archer suite was deleted,
# so it should not participate in the per-example coverage drift guard.
_FIXTURE_ONLY_EXAMPLES: frozenset[str] = frozenset({"archer"})


def test_every_agent_has_a_dedicated_test_file() -> None:
    """
    Walk every agent root and assert each agent has either a
    matching ``test_example_<name>.py`` file or an entry in
    :data:`_ALT_COVERED`. Also flag orphaned test files whose
    ``<name>`` no longer matches any agent.

    :raises AssertionError: When an agent is missing coverage
        or a test file points at a removed agent.
    """
    repo_root = Path(__file__).resolve().parents[3]
    e2e_dir = repo_root / "tests" / "e2e" / "omnigent"

    # Agent roots. These MUST stay in lock-step with the resolution
    # order in ``_example_helpers.example_yaml_path`` (see the module
    # docstring): the helper resolves ``examples/``,
    # ``tests/resources/examples/`` (single-YAML + dir-shaped), and
    # ``tests/resources/agents/`` — so the guard must scan all three,
    # or a real per-example test in an un-scanned root reads as an
    # orphan. Both ``examples/`` and ``tests/resources/examples/``
    # carry shipped/demo agents, so each contributes dir-shaped
    # AGENTSPEC bundles (``config.yaml`` required, to exclude non-agent
    # dirs) and single-YAML demos (content-filtered to real specs).
    on_disk: set[str] = set()
    on_disk |= _scan_agent_root(repo_root / "examples", require_config_yaml=True)
    on_disk |= (
        _scan_agent_root(repo_root / "tests" / "resources" / "examples", require_config_yaml=True)
        - _FIXTURE_ONLY_EXAMPLES
    )
    # Test-only fixture agents under ``tests/resources/agents/`` —
    # any directory counts (single-file bundles are valid here).
    on_disk |= _scan_agent_root(
        repo_root / "tests" / "resources" / "agents", require_config_yaml=False
    )

    # Pick up existing tests by file-name convention.
    named_covered: set[str] = set()
    for p in e2e_dir.iterdir():
        name = p.name
        if name.startswith("test_example_") and name.endswith(".py"):
            # Strip prefix+suffix: ``test_example_<name>.py`` -> ``<name>``.
            named_covered.add(name[len("test_example_") : -len(".py")])

    missing = on_disk - named_covered - _ALT_COVERED
    assert missing == set(), (
        f"Agents without a dedicated test file: {sorted(missing)}. "
        f"Create tests/e2e/omnigent/test_example_<name>.py for "
        f"each, or add the name to _ALT_COVERED above if coverage "
        f"lives in a differently-named test file."
    )

    # Agents whose test_example_<name>.py exists but the agent
    # directory was removed (or never committed). Kept as an
    # allowlist so the orphan check doesn't block CI while the
    # agent is pending restoration.
    _ORPHAN_ALLOWED: frozenset[str] = frozenset(
        {
            # test_example_omni.py exists but tests/resources/agents/omni
            # was never committed. The test itself also fails
            # (FileNotFoundError). Allowlisted until the resource is
            # created or the test is deleted.
            "omni",
        }
    )
    stale = named_covered - on_disk - _ORPHAN_ALLOWED
    assert stale == set(), (
        f"Orphaned test_example_<name>.py files for agents that "
        f"no longer exist in examples/ or "
        f"tests/resources/agents/: {sorted(stale)}. Delete the "
        f"test file or restore the agent."
    )
