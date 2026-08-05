"""Shared helpers for the per-example e2e tests under ``test_example_*.py``.

Each example under ``examples/`` has its own test file that
exercises that example's intended functionality through the real
``omnigent run`` subprocess. The helpers in this module keep the
per-file boilerplate minimal: resolve the YAML path, build argv,
run subprocess, assert common invariants.

Not a conftest because these are called explicitly by test functions
(not picked up as fixtures).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests._model_pools import resolve_model
from tests.e2e._run_with_group_timeout import run_with_group_timeout

# --------------------------------------------------------------------
# Env vars for unblocking MCP-gated example tests in real-run mode.
#
# Several examples (databricks_mcps_agent,
# databricks_mcps_agent_with_google_policy, glean_mcp_agent,
# databricks_coding_agent) hardcode
#   args: [-m, omnigent.inner.databricks_mcps.<X>, --profile,
#          <profile-name>]
# in their YAMLs. The Databricks MCP subprocess has three auth
# modes (see omnigent/inner/databricks_mcps/common/workspace.py):
#   1. ``--host <url> --token <PAT>``  (explicit PAT)
#   2. ``--profile <name>``            (look up host+token in .databrickscfg)
#   3. no args                         (fall through to env vars)
# The default YAML args target mode 2, which requires the host to
# already have that profile's section in its ``~/.databrickscfg``.
# These env vars let a test host override to mode 1 or 2 without
# editing the YAML in-tree.
#
# Usage — mode 2 (profile-based):
#   OMNIGENT_E2E_MCP_PROFILE=<your-profile> pytest ...
#
# Usage — mode 1 (direct PAT, no profile needed):
#   OMNIGENT_E2E_MCP_HOST=https://... \
#   OMNIGENT_E2E_MCP_TOKEN=dapi... \
#   pytest ...
#
# When none of the three vars are set, tests fall back to
# structural validation (spec + AgentDef translation).
ENV_MCP_PROFILE = "OMNIGENT_E2E_MCP_PROFILE"
ENV_MCP_HOST = "OMNIGENT_E2E_MCP_HOST"
ENV_MCP_TOKEN = "OMNIGENT_E2E_MCP_TOKEN"

# Default low-effort prompt used by examples whose purpose is to
# demonstrate a feature but not run heavy tool logic on every call.
# Per-example tests override this for features that only fire with a
# specific prompt shape.
DEFAULT_PROMPT = "Reply with just the word 'OK'."

# Default harness + model when a YAML doesn't pin one. We prefer
# the openai-agents harness against dogfood GPT-5-mini because it
# honors OPENAI_BASE_URL / OPENAI_API_KEY env vars directly (no
# ~/.databrickscfg patching), which matches how the rest of the e2e
# suite authenticates.
DEFAULT_HARNESS = "openai-agents"
DEFAULT_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)

# Subprocess wall-clock budget. The openai-agents harness against
# dogfood typically finishes a one-turn reply in 5-15s; 180s covers
# cold-start + slow days.
RUN_TIMEOUT_SEC = 180


def example_yaml_path(repo_root: Path, name: str) -> Path:
    """
    Return the YAML / config file path for an example agent.

    Resolution order (first hit wins):

    1. ``examples/<name>.yaml`` — shipped examples that remain in
       the top-level ``examples/`` directory.
    2. ``tests/resources/examples/<name>.yaml`` — single-YAML demos
       moved to the test-resources tree.
    3. ``tests/resources/examples/<name>/`` — multi-file AGENTSPEC
       demos (``archer``, ``coder``, ``openai-coder``).
    4. ``tests/resources/agents/<name>/`` — test-only fixtures
       (aspirational specs, incremental-feature variants).

    :param repo_root: Unified repo root; typically the
        ``omnigent_repo_root`` fixture value.
    :param name: Agent name. For top-level YAMLs, the filename
        stem (``"hello_world"`` → ``tests/resources/examples/hello_world.yaml``);
        for dir-shaped agents, the directory name (``"archer"``).
    :returns: Absolute :class:`Path` to the YAML / config file.
    :raises FileNotFoundError: When none of the layouts match in
        any of the roots.
    """
    # Shipped examples still in examples/ (1).
    top_level = repo_root / "examples" / f"{name}.yaml"
    if top_level.is_file():
        return top_level
    # Test-resource single-YAML demos (2).
    res_top = repo_root / "tests" / "resources" / "examples" / f"{name}.yaml"
    if res_top.is_file():
        return res_top
    # Dir-shaped demos + test fixtures (3, 4).
    for root in (
        repo_root / "tests" / "resources" / "examples",
        repo_root / "tests" / "resources" / "agents",
    ):
        agent_dir = root / name
        legacy = agent_dir / f"{name}.yaml"
        if legacy.is_file():
            return legacy
        agentspec = agent_dir / "config.yaml"
        if agentspec.is_file():
            return agentspec
    raise FileNotFoundError(
        f"No YAML for agent {name!r} — checked "
        f"examples/{name}.yaml, "
        f"tests/resources/examples/{name}.yaml, "
        f"tests/resources/examples/{name}/ and tests/resources/agents/{name}/ "
        f"for both LEGACY ({name}.yaml) and AGENTSPEC (config.yaml) "
        f"layouts."
    )


def run_one_shot(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    example_name: str,
    prompt: str = DEFAULT_PROMPT,
    harness: str | None = DEFAULT_HARNESS,
    model: str | None = DEFAULT_MODEL,
) -> subprocess.CompletedProcess[str]:
    """
    Invoke ``omnigent run <yaml> -p <prompt>`` one-shot.

    :param omnigent_python: Interpreter with omnigent + required
        SDKs installed. Provided by the ``omnigent_python`` fixture.
    :param omnigent_repo_root: Cwd so the YAML's ``callable:``
        dotted paths resolve via repo-root-on-sys.path. Provided by
        the ``omnigent_repo_root`` fixture.
    :param omnigent_credentials_env: PAT + BASE_URL + profile env
        populated from ``--llm-api-key``. Provided by the
        ``omnigent_credentials_env`` fixture.
    :param example_name: Agent name; see :func:`example_yaml_path`
        for resolution order (top-level YAML, AGENTSPEC dir under
        ``examples/``, or test fixture under
        ``tests/resources/agents/``).
    :param prompt: User message; default ``DEFAULT_PROMPT``.
    :param harness: ``--harness`` value, or ``None`` to let the
        YAML's ``executor.type`` win (used when the YAML pins a
        specific harness like ``claude_sdk``).
    :param model: Optional ``--model`` override, independent of whether
        the YAML or the CLI selects the harness.
    :returns: The completed subprocess. Caller decides which
        fields to assert.
    """
    yaml_path = example_yaml_path(omnigent_repo_root, example_name)
    return run_one_shot_at_path(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=omnigent_credentials_env,
        yaml_path=yaml_path,
        prompt=prompt,
        harness=harness,
        model=model,
    )


def run_one_shot_at_path(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    yaml_path: Path,
    prompt: str = DEFAULT_PROMPT,
    harness: str | None = DEFAULT_HARNESS,
    model: str | None = DEFAULT_MODEL,
) -> subprocess.CompletedProcess[str]:
    """
    Like :func:`run_one_shot` but takes an arbitrary ``yaml_path``
    (a file or AGENTSPEC directory) rather than an
    ``examples/<name>`` lookup.

    Needed by tests that materialize a rewritten YAML in
    ``tmp_path`` (e.g. MCP-profile overrides, omni endpoint
    rewrites).

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Cwd (repo root so module lookups
        land).
    :param omnigent_credentials_env: Env.
    :param yaml_path: Absolute path to the YAML file or directory.
    :param prompt: User message.
    :param harness: ``--harness`` value or ``None``.
    :param model: ``--model`` value or ``None``.
    :returns: The completed subprocess.
    """
    argv: list[str] = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "-p",
        prompt,
        "--no-log",
        "--no-session",
    ]
    if harness is not None:
        argv.extend(["--harness", harness])
    if model is not None:
        argv.extend(["--model", model])
    # run_with_group_timeout, not subprocess.run: grandchildren
    # (server / runner / harness) hold the pipes past timeout.
    return run_with_group_timeout(
        argv,
        env=omnigent_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SEC,
    )


def validate_agent_def_structure(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    example_name: str,
    expected_name: str,
    expected_tools: set[str] | None = None,
    expected_policies: set[str] | None = None,
    expected_executor_type: str | None = None,
    expected_executor_harness: str | None = None,
    expected_terminals: set[str] | None = None,
    expected_os_env_type: str | None = None,
) -> None:
    """
    Parse + translate an example YAML and assert the resulting
    :class:`AgentDef` has the structural shape we expect.

    Used for examples that can't run end-to-end on a laptop
    (hosted MCP servers needing OAuth, live Glean / Google
    profiles) but whose *spec translation* we still want to
    guard against regressions. Exercises the unified spec parser,
    the :func:`agent_spec_to_agent_def` translator, and the
    per-tool / per-policy / per-terminal registration paths —
    all of which the unification touched.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Cwd so module-path resolution
        matches the CLI.
    :param example_name: Example directory under ``examples/``.
    :param expected_name: ``AgentDef.name`` must equal this
        exactly, e.g. ``"databricks_mcps_agent"``.
    :param expected_tools: Tool names that must be present on
        ``AgentDef.tools`` (subset-match; extras don't fail).
        ``None`` means skip the tool check.
    :param expected_policies: Policy names (as declared in the
        YAML's ``policies:`` dict). ``None`` skips.
    :param expected_executor_type: ``AgentDef.executor.type``
        when set by the YAML. ``None`` skips.
    :param expected_executor_harness: ``AgentDef.executor.harness``.
        ``None`` skips.
    :param expected_terminals: Names that must be present in
        ``AgentDef.terminals``.
    :param expected_os_env_type: ``AgentDef.os_env.type`` when
        set. ``None`` skips.
    """
    yaml_path = example_yaml_path(omnigent_repo_root, example_name)
    # Build a snippet that loads the agent def, then prints a
    # small JSON summary we can assert against. Using JSON
    # instead of a bunch of ``assert``s inside the snippet keeps
    # the failure message at the pytest-side under our control.
    snippet = f"""
import json
import sys
sys.path.insert(0, {str(omnigent_repo_root)!r})
from omnigent.inner.loader import load_agent_def_from_path

agent_def = load_agent_def_from_path({str(yaml_path)!r})
assert agent_def is not None, "load returned None"

tool_names = sorted(agent_def.tools.keys()) if agent_def.tools else []
policy_names = (
    sorted(agent_def.policies.keys())
    if getattr(agent_def, "policies", None)
    else []
)
terminals = (
    sorted(agent_def.terminals.keys())
    if getattr(agent_def, "terminals", None)
    else []
)
executor = agent_def.executor
os_env = agent_def.os_env
summary = {{
    "name": agent_def.name,
    "tools": tool_names,
    "policies": policy_names,
    "terminals": terminals,
    "executor_type": getattr(executor, "type", None) if executor else None,
    "executor_harness": (
        getattr(executor, "harness", None) if executor else None
    ),
    "os_env_type": getattr(os_env, "type", None) if os_env else None,
}}
print("SUMMARY:" + json.dumps(summary))
"""
    result = subprocess.run(
        [str(omnigent_python), "-c", snippet],
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Structural check failed for {example_name!r}:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # Extract the JSON summary — other print()s (e.g. DBOS init
    # noise) may precede it.
    import json

    summary_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("SUMMARY:")),
        None,
    )
    assert summary_line is not None, (
        f"Didn't reach the SUMMARY print in the load snippet for "
        f"{example_name!r}. stdout={result.stdout!r}"
    )
    summary = json.loads(summary_line[len("SUMMARY:") :])

    assert summary["name"] == expected_name, (
        f"{example_name!r}: AgentDef.name expected {expected_name!r}, got {summary['name']!r}"
    )

    if expected_tools is not None:
        actual_tools = set(summary["tools"])
        missing = expected_tools - actual_tools
        assert not missing, (
            f"{example_name!r}: expected tools missing from AgentDef: "
            f"{sorted(missing)}. Got: {sorted(actual_tools)}"
        )

    if expected_policies is not None:
        actual_policies = set(summary["policies"])
        missing = expected_policies - actual_policies
        assert not missing, (
            f"{example_name!r}: expected policies missing from "
            f"AgentDef: {sorted(missing)}. Got: {sorted(actual_policies)}"
        )

    if expected_terminals is not None:
        actual_terminals = set(summary["terminals"])
        missing = expected_terminals - actual_terminals
        assert not missing, (
            f"{example_name!r}: expected terminals missing from "
            f"AgentDef: {sorted(missing)}. Got: {sorted(actual_terminals)}"
        )

    if expected_executor_type is not None:
        assert summary["executor_type"] == expected_executor_type, (
            f"{example_name!r}: executor.type expected "
            f"{expected_executor_type!r}, got {summary['executor_type']!r}"
        )

    if expected_executor_harness is not None:
        assert summary["executor_harness"] == expected_executor_harness, (
            f"{example_name!r}: executor.harness expected "
            f"{expected_executor_harness!r}, got "
            f"{summary['executor_harness']!r}"
        )

    if expected_os_env_type is not None:
        assert summary["os_env_type"] == expected_os_env_type, (
            f"{example_name!r}: os_env.type expected "
            f"{expected_os_env_type!r}, got {summary['os_env_type']!r}"
        )


def assert_completed_one_shot(
    result: subprocess.CompletedProcess[str],
    example_name: str,
) -> None:
    """
    Assert a one-shot ``omnigent run`` finished cleanly.

    :param result: The completed subprocess, as returned by
        :func:`run_one_shot`.
    :param example_name: Example name; used only in error messages
        so a failure inside a parametrized-like test file still
        names the specific example.
    """
    assert result.returncode == 0, (
        f"{example_name!r}: omnigent run exited with "
        f"{result.returncode} (expected 0).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Any assistant reply must reach stdout — zero-length stdout
    # means the run exited 0 without actually streaming anything.
    # --no-log strips the banner, so stdout == "" is a regression.
    assert result.stdout.strip(), (
        f"{example_name!r}: run exited 0 but produced no stdout. stderr:\n{result.stderr}"
    )


@dataclass(frozen=True)
class McpAuthOverride:
    """
    MCP subprocess authentication override read from env vars.

    The Databricks MCP CLI entry points accept three mutually
    exclusive auth shapes: ``--profile <name>`` (looks up host +
    token in ~/.databrickscfg), ``--host <url> --token <PAT>``
    (explicit PAT), or nothing (env var fallback). This object
    captures which shape the test is requesting.

    :param profile: Profile name for ``--profile`` mode, or None.
    :param host: Workspace URL for ``--host/--token`` mode, or None.
    :param token: PAT for ``--host/--token`` mode, or None.
    """

    profile: str | None
    host: str | None
    token: str | None

    @property
    def has_any_override(self) -> bool:
        """True when the env vars requested either auth mode."""
        if self.profile is not None:
            return True
        return self.host is not None and self.token is not None


def mcp_auth_override() -> McpAuthOverride:
    """
    Read MCP subprocess auth override from env vars.

    If ``OMNIGENT_E2E_MCP_PROFILE`` is set, returns a
    profile-based override. Else if BOTH ``OMNIGENT_E2E_MCP_HOST``
    and ``OMNIGENT_E2E_MCP_TOKEN`` are set, returns a PAT-based
    override. Otherwise returns an empty override (caller falls
    back to structural validation).

    :returns: :class:`McpAuthOverride`. Check
        ``.has_any_override`` to branch.
    """
    profile = os.environ.get(ENV_MCP_PROFILE) or None
    host = os.environ.get(ENV_MCP_HOST) or None
    token = os.environ.get(ENV_MCP_TOKEN) or None
    # Profile takes priority. PAT requires both host + token.
    if profile:
        return McpAuthOverride(profile=profile, host=None, token=None)
    if host and token:
        return McpAuthOverride(profile=None, host=host, token=token)
    return McpAuthOverride(profile=None, host=None, token=None)


def _rewrite_mcp_auth_in_place(yaml_obj: object, override: McpAuthOverride) -> int:
    """
    Walk a parsed YAML structure and rewrite the auth args for
    every ``-m omnigent.inner.databricks_mcps.*`` subprocess.

    Strips any existing ``--profile / --host / --token`` arg
    pairs from the MCP's ``args:`` list and appends the ones
    requested by *override*.

    :param yaml_obj: Parsed YAML (nested dict/list/scalar).
    :param override: The auth mode to inject.
    :returns: Number of MCP entries rewritten.
    """
    count = 0
    if isinstance(yaml_obj, dict):
        for key, value in yaml_obj.items():
            if key == "args" and isinstance(value, list):
                count += _rewrite_args_list(value, override)
            else:
                count += _rewrite_mcp_auth_in_place(value, override)
    elif isinstance(yaml_obj, list):
        for item in yaml_obj:
            count += _rewrite_mcp_auth_in_place(item, override)
    return count


def _rewrite_args_list(args: list[object], override: McpAuthOverride) -> int:
    """
    Rewrite a single MCP ``args:`` list: strip any existing
    ``--profile / --host / --token`` pairs and append the
    override's auth flags.

    :param args: The ``args`` list to mutate in place.
    :param override: The auth shape to inject.
    :returns: 1 if this list was an MCP-subprocess args list, else 0.
    """
    if not any(
        isinstance(x, str) and x.startswith("omnigent.inner.databricks_mcps.") for x in args
    ):
        return 0

    # Strip (flag, value) pairs for any of the three auth flags.
    strip_flags = {"--profile", "--host", "--token"}
    cleaned: list[object] = []
    i = 0
    while i < len(args):
        if args[i] in strip_flags and i + 1 < len(args):
            i += 2  # skip both the flag and its value
            continue
        cleaned.append(args[i])
        i += 1

    # Append the override's auth args.
    if override.profile is not None:
        cleaned.extend(["--profile", override.profile])
    elif override.host is not None and override.token is not None:
        cleaned.extend(["--host", override.host, "--token", override.token])
    # (If override has neither, leave args bare — MCP subprocess
    # will fall through to DATABRICKS_* env vars.)

    args.clear()
    args.extend(cleaned)
    return 1


def materialize_yaml_with_mcp_auth(
    source: Path, dest_dir: Path, override: McpAuthOverride
) -> Path:
    """
    Copy *source* into *dest_dir* and rewrite every MCP
    subprocess's auth args to match *override*.

    Returns the path to pass to ``omnigent run``:
    - For a single-file YAML source, the rewritten single-file
      YAML at ``<dest_dir>/<name>``.
    - For a directory source (AGENTSPEC), a new AGENTSPEC
      directory at ``<dest_dir>/<name>/`` with the rewritten
      ``config.yaml`` inside.

    :param source: YAML file or AGENTSPEC directory.
    :param dest_dir: Parent directory to write the rewrite into.
    :param override: The auth shape to inject into every MCP's
        args list.
    :returns: Path to pass to the CLI.
    :raises AssertionError: When no MCP subprocess was found in
        the YAML — the test expected at least one, so zero
        rewrites means the YAML shape drifted.
    """
    if source.is_file():
        raw = yaml.safe_load(source.read_text())
        count = _rewrite_mcp_auth_in_place(raw, override)
        assert count > 0, (
            f"materialize_yaml_with_mcp_auth found no MCP subprocess to rewrite in {source}."
        )
        out = dest_dir / source.name
        out.write_text(yaml.safe_dump(raw, default_flow_style=False))
        return out

    import shutil as _shutil

    target_dir = dest_dir / source.name
    _shutil.copytree(source, target_dir)
    cfg_path = target_dir / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    count = _rewrite_mcp_auth_in_place(raw, override)
    assert count > 0, (
        f"materialize_yaml_with_mcp_auth found no MCP subprocess to rewrite in {cfg_path}."
    )
    cfg_path.write_text(yaml.safe_dump(raw, default_flow_style=False))
    return target_dir


def require_claude_sdk() -> None:
    """
    Fail loud when the ``claude_agent_sdk`` package is missing.

    Examples that pin ``executor.type: claude_sdk`` import the
    upstream SDK at turn time; a missing package surfaces mid-run
    with an ImportError that obscures the root cause. We detect
    it upfront so the failure message names the missing dependency.
    """
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.fail(
            "This example requires the 'claude-agent-sdk' Python "
            "package. Install it via the [claude-code] extra or "
            "explicitly: pip install claude-agent-sdk"
        )


def require_codex_cli() -> None:
    """
    Fail loud when the ``codex`` CLI binary is missing on PATH.

    Required by examples that pin ``harness: codex`` for any
    worker agent.
    """
    if shutil.which("codex") is None:
        pytest.fail(
            "codex CLI required but not on PATH. Install per the "
            "Codex project README before running this test."
        )
