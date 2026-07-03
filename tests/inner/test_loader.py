"""Tests for YAML / dict loading."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.datamodel import ExecutorSpec, OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.loader import load_agent_def
from omnigent.inner.policies import FunctionPolicy, PromptPolicy
from omnigent.inner.tools import (
    AgentTool,
    CancellableFunctionTool,
    FunctionTool,
    HandoffTool,
    InheritedTool,
    MCPTool,
    SkillTool,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Local runner-protocol fixture ────────────────────────
#
# The inner stack still supports ``type: cancellable_function``
# YAMLs that point at runner-protocol objects. The test file
# carries its own minimal runner instance instead of depending
# on ``tests.resources.examples._shared.tool_functions`` (which is now plain-
# callable only after step (c) — see
# designs/SERVER_HARNESS_CONTRACT.md).


class _TestSleepRunner:
    """Runner-protocol stub for the cancellable_function loader test."""

    def start(self, args: object, on_complete: object) -> None:
        """Stub — never called by the loader test."""
        raise NotImplementedError


sleep_runner = _TestSleepRunner()


class TestLoadFromDict(unittest.TestCase):
    def test_minimal(self):
        a = load_agent_def({"name": "test", "prompt": "Hello."})
        self.assertEqual(a.name, "test")
        self.assertEqual(a.prompt, "Hello.")

    def test_executor_string(self):
        self.assertEqual(
            load_agent_def({"name": "t", "executor": "gpt-5"}).executor.model, "gpt-5"
        )

    def test_executor_dict(self):
        a = load_agent_def({"name": "t", "executor": {"model": "claude-sonnet-4"}})
        self.assertEqual(a.executor.model, "claude-sonnet-4")

    def test_tools_function(self):
        a = load_agent_def(
            {"name": "t", "tools": {"f": {"type": "function", "catalog_path": "a.b.c"}}}
        )
        self.assertIsInstance(a.tools["f"], FunctionTool)
        self.assertEqual(a.tools["f"].catalog_path, "a.b.c")

    def test_tools_cancellable_function(self):
        a = load_agent_def(
            {
                "name": "t",
                "tools": {
                    "sleep": {
                        "type": "cancellable_function",
                        "runner": "tests.inner.test_loader.sleep_runner",
                    }
                },
            }
        )
        self.assertIsInstance(a.tools["sleep"], CancellableFunctionTool)
        self.assertTrue(a.tools["sleep"].cancellable)
        self.assertIsNotNone(a.tools["sleep"].runner)

    def test_tools_mcp(self):
        a = load_agent_def({"name": "t", "tools": {"s": {"type": "mcp", "url": "http://x"}}})
        self.assertIsInstance(a.tools["s"], MCPTool)
        self.assertEqual(a.tools["s"].url, "http://x")

    def test_tools_agent(self):
        a = load_agent_def(
            {
                "name": "t",
                "tools": {"h": {"type": "agent", "prompt": "Help.", "tools": {"s": "inherit"}}},
            }
        )
        self.assertIsInstance(a.tools["h"], AgentTool)
        self.assertIsInstance(a.tools["h"].tools["s"], InheritedTool)

    def test_tools_agent_with_executor(self):
        a = load_agent_def(
            {
                "name": "t",
                "tools": {
                    "h": {
                        "type": "agent",
                        "prompt": "Help.",
                        "executor": {
                            "model": "databricks-claude-sonnet-4",
                            "harness": "claude-sdk",
                            "profile": "test-profile",
                        },
                    }
                },
            }
        )
        self.assertIsInstance(a.tools["h"], AgentTool)
        self.assertEqual(
            a.tools["h"].executor,
            ExecutorSpec(
                model="databricks-claude-sonnet-4",
                harness="claude-sdk",
                profile="test-profile",
            ),
        )

    def test_tools_agent_with_inherited_os_env(self):
        a = load_agent_def(
            {
                "name": "t",
                "tools": {
                    "h": {
                        "type": "agent",
                        "prompt": "Help.",
                        "os_env": "inherit",
                    }
                },
            }
        )
        self.assertIsInstance(a.tools["h"], AgentTool)
        self.assertEqual(a.tools["h"].os_env, "inherit")

    def test_tools_agent_max_sessions(self):
        a = load_agent_def(
            {
                "name": "t",
                "tools": {
                    "h": {
                        "type": "agent",
                        "prompt": "Help.",
                        "max_sessions": 3,
                    }
                },
            }
        )
        self.assertIsInstance(a.tools["h"], AgentTool)
        self.assertEqual(a.tools["h"].max_sessions, 3)

    def test_tools_agent_max_sessions_defaults_to_none(self):
        a = load_agent_def({"name": "t", "tools": {"h": {"type": "agent", "prompt": "Help."}}})
        self.assertIsInstance(a.tools["h"], AgentTool)
        self.assertIsNone(a.tools["h"].max_sessions)

    def test_tools_agent_max_sessions_valid_integer(self):
        """Valid positive integers are accepted and stored unchanged."""
        for value in (1, 5, 100):
            with self.subTest(value=value):
                a = load_agent_def(
                    {
                        "name": "t",
                        "tools": {"h": {"type": "agent", "prompt": ".", "max_sessions": value}},
                    }
                )
                self.assertEqual(a.tools["h"].max_sessions, value)

    def test_tools_agent_max_sessions_invalid_string_raises(self):
        """A string value for max_sessions must raise a ValueError naming the tool and field."""
        with self.assertRaises(ValueError) as ctx:
            load_agent_def(
                {
                    "name": "t",
                    "tools": {"my_tool": {"type": "agent", "prompt": ".", "max_sessions": "3"}},
                }
            )
        msg = str(ctx.exception)
        self.assertIn("my_tool", msg)
        self.assertIn("max_sessions", msg)

    def test_tools_agent_max_sessions_invalid_float_raises(self):
        """A float value for max_sessions must raise a ValueError naming the tool and field."""
        with self.assertRaises(ValueError) as ctx:
            load_agent_def(
                {
                    "name": "t",
                    "tools": {"my_tool": {"type": "agent", "prompt": ".", "max_sessions": 2.5}},
                }
            )
        msg = str(ctx.exception)
        self.assertIn("my_tool", msg)
        self.assertIn("max_sessions", msg)

    def test_tools_agent_max_sessions_zero_raises(self):
        """Zero must raise a ValueError naming the tool and field."""
        with self.assertRaises(ValueError) as ctx:
            load_agent_def(
                {
                    "name": "t",
                    "tools": {"my_tool": {"type": "agent", "prompt": ".", "max_sessions": 0}},
                }
            )
        msg = str(ctx.exception)
        self.assertIn("my_tool", msg)
        self.assertIn("max_sessions", msg)

    def test_tools_agent_max_sessions_negative_raises(self):
        """Negative integers must raise a ValueError naming the tool and field."""
        with self.assertRaises(ValueError) as ctx:
            load_agent_def(
                {
                    "name": "t",
                    "tools": {"my_tool": {"type": "agent", "prompt": ".", "max_sessions": -1}},
                }
            )
        msg = str(ctx.exception)
        self.assertIn("my_tool", msg)
        self.assertIn("max_sessions", msg)

    def test_tools_inherit(self):
        self.assertIsInstance(
            load_agent_def({"name": "t", "tools": {"x": "inherit"}}).tools["x"], InheritedTool
        )

    def test_tools_skill(self):
        a = load_agent_def(
            {"name": "t", "tools": {"d": {"type": "skill", "content": "./skills/d"}}}
        )
        self.assertIsInstance(a.tools["d"], SkillTool)
        self.assertEqual(a.tools["d"].path, "./skills/d")

    def test_tools_handoff(self):
        a = load_agent_def(
            {"name": "t", "tools": {"e": {"type": "handoff", "target_agent": "billing"}}}
        )
        self.assertIsInstance(a.tools["e"], HandoffTool)
        self.assertEqual(a.tools["e"].target_agent, "billing")

    def test_policies_function(self):
        a = load_agent_def(
            {"name": "t", "policies": {"p": {"type": "function", "on": ["request"]}}},
        )
        self.assertIsInstance(a.policies["p"], FunctionPolicy)
        self.assertEqual(a.policies["p"].on, ["request"])

    def test_policies_function_with_factory_params(self):
        a = load_agent_def(
            {
                "name": "t",
                "policies": {
                    "c": {
                        "type": "function",
                        "on": ["tool_call"],
                        "handler": (
                            "tests.resources.examples._shared"
                            ".rate_limit_policy.max_tool_calls_per_turn"
                        ),
                        "factory_params": {"limit": 10},
                    }
                },
            }
        )
        self.assertIsInstance(a.policies["c"], FunctionPolicy)
        self.assertEqual(a.policies["c"].factory_params, {"limit": 10})
        self.assertIsNotNone(a.policies["c"].callable)
        self.assertIsNotNone(a.policies["c"].factory)

    def test_policies_function_with_empty_factory_params(self):
        """Empty factory_params: {} should still trigger factory invocation (zero-arg factory)."""
        a = load_agent_def(
            {
                "name": "t",
                "policies": {
                    "c": {
                        "type": "function",
                        "on": ["tool_call"],
                        "handler": (
                            "tests.resources.examples._shared"
                            ".rate_limit_policy.max_tool_calls_per_turn"
                        ),
                        "factory_params": {},
                    }
                },
            }
        )
        self.assertIsInstance(a.policies["c"], FunctionPolicy)
        self.assertIsNotNone(a.policies["c"].factory)
        self.assertIsNotNone(a.policies["c"].callable)
        # The factory should have been called with defaults (limit=10)
        self.assertNotEqual(a.policies["c"].factory, a.policies["c"].callable)

    def test_policies_prompt(self):
        a = load_agent_def(
            {
                "name": "t",
                "policies": {"s": {"type": "prompt", "on": ["response"], "prompt": "Safe?"}},
            }
        )
        self.assertIsInstance(a.policies["s"], PromptPolicy)

    def test_policies_prompt_executor_string(self):
        a = load_agent_def(
            {
                "name": "t",
                "policies": {
                    "s": {
                        "type": "prompt",
                        "on": ["response"],
                        "prompt": "Safe?",
                        "executor": "gpt-5-mini",
                    }
                },
            }
        )
        self.assertEqual(a.policies["s"].executor, ExecutorSpec(model="gpt-5-mini"))

    def test_policies_prompt_executor_dict(self):
        a = load_agent_def(
            {
                "name": "t",
                "policies": {
                    "s": {
                        "type": "prompt",
                        "on": ["response"],
                        "prompt": "Safe?",
                        "executor": {
                            "model": "gpt-5-mini",
                            "harness": "open-responses",
                            "profile": "policy",
                        },
                    }
                },
            }
        )
        self.assertEqual(
            a.policies["s"].executor,
            ExecutorSpec(
                model="gpt-5-mini",
                harness="open-responses",
                profile="policy",
            ),
        )

    def test_memories(self):
        a = load_agent_def(
            {"name": "t", "memories": {"p": {"scope": "per_user"}, "s": {"scope": "cross_user"}}}
        )
        self.assertEqual(a.memories["p"].scope, "per_user")
        self.assertEqual(a.memories["s"].scope, "cross_user")

    def test_runtime_flag(self):
        self.assertTrue(load_agent_def({"name": "t", "runtime": True}).runtime)

    def test_async_flag_defaults_true(self):
        self.assertTrue(load_agent_def({"name": "t"}).async_enabled)

    def test_async_flag_from_dict(self):
        self.assertFalse(load_agent_def({"name": "t", "async": False}).async_enabled)

    def test_cancellable_flag_from_dict(self):
        self.assertFalse(load_agent_def({"name": "t", "cancellable": False}).cancellable)

    def test_async_enabled_alias_from_dict(self):
        self.assertFalse(load_agent_def({"name": "t", "async_enabled": False}).async_enabled)

    def test_os_env_string(self):
        a = load_agent_def({"name": "t", "os_env": "caller_process"})
        self.assertEqual(a.os_env, OSEnvSpec(type="caller_process"))

    def test_os_env_dict(self):
        a = load_agent_def(
            {
                "name": "t",
                "os_env": {
                    "type": "caller_process",
                    "cwd": "/tmp/work",
                    "sandbox": {
                        "type": "none",
                    },
                },
            }
        )
        self.assertEqual(
            a.os_env,
            OSEnvSpec(
                type="caller_process",
                cwd="/tmp/work",
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
        )

    def test_params(self):
        a = load_agent_def(
            {"name": "t", "params": {"u": {"type": "string", "description": "User"}}}
        )
        self.assertEqual(a.params["u"].type, "string")

    def test_workflow(self):
        self.assertEqual(
            load_agent_def({"name": "t", "workflow": "a -> b -> c"}).workflow, "a -> b -> c"
        )


class TestLoadFromYAML(unittest.TestCase):
    def test_yaml_on_key_is_not_parsed_as_boolean(self):
        yaml_content = """
name: policy_agent
runtime: true
policies:
  block_sleep:
    type: function
    on: [tool_call]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                a = load_agent_def(f.name)
                self.assertEqual(a.policies["block_sleep"].on, ["tool_call"])
                self.assertTrue(a.runtime)
            finally:
                os.unlink(f.name)

    def test_load_yaml_file(self):
        yaml_content = """
name: data_analyst
prompt: Answer questions using data.
async: false
executor:
  model: databricks-claude-sonnet-4
tools:
  sql_query:
    type: mcp
    url: http://localhost:8080
  table_search:
    type: agent
    prompt: Search for tables.
    tools:
      sql_query: inherit
policies:
  cost_limit:
    type: function
    on: [tool_call]
    callable: tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn
    factory_params:
      limit: 20
memories:
  user_prefs:
    scope: per_user
runtime: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                a = load_agent_def(f.name)
                self.assertEqual(a.name, "data_analyst")
                self.assertEqual(a.executor.model, "databricks-claude-sonnet-4")
                self.assertIsInstance(a.tools["sql_query"], MCPTool)
                self.assertIsInstance(a.tools["table_search"], AgentTool)
                self.assertIsInstance(a.tools["table_search"].tools["sql_query"], InheritedTool)
                self.assertIsInstance(a.policies["cost_limit"], FunctionPolicy)
                self.assertEqual(a.policies["cost_limit"].on, ["tool_call"])
                self.assertEqual(a.memories["user_prefs"].scope, "per_user")
                self.assertFalse(a.async_enabled)
                self.assertTrue(a.runtime)
            finally:
                os.unlink(f.name)

    def test_load_yaml_file_with_command_mcp(self):
        yaml_content = """
name: docs_agent
prompt: Answer questions using docs.
tools:
  google:
    type: mcp
    command: python3.10
    args:
      - /home/matei/mcp/servers/google_mcp/google_mcp_deploy.pex
    tools:
      - google_docs_search
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                a = load_agent_def(f.name)
                self.assertIsInstance(a.tools["google"], MCPTool)
                self.assertEqual(a.tools["google"].command, "python3.10")
                self.assertEqual(
                    a.tools["google"].args,
                    ["/home/matei/mcp/servers/google_mcp/google_mcp_deploy.pex"],
                )
                self.assertEqual(a.tools["google"].tools, ["google_docs_search"])
            finally:
                os.unlink(f.name)


class TestInstructionsField(unittest.TestCase):
    """
    ``instructions:`` field handling in omnigent-flavored YAML.

    Native Omnigent YAMLs have always supported ``instructions: <path>``
    (path relative to the bundle dir, falling through to inline
    text if not a file). Omnigent-flavored YAMLs silently
    dropped the field — the loader didn't read it, the translator
    didn't see it. Bug from kasey_uhlenhuth's report. These tests
    pin the cross-format parity.
    """

    def test_instructions_resolves_relative_path_to_file_contents(self):
        """``instructions: foo.md`` reads foo.md sitting next to the YAML."""
        yaml_content = "name: instr_agent\nprompt: dummy\ninstructions: AGENTS.md\n"
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "agent.yaml"
            yaml_path.write_text(yaml_content)
            (Path(td) / "AGENTS.md").write_text("REPLY ONLY: SAUCE")
            a = load_agent_def(str(yaml_path))
            self.assertEqual(a.instructions, "REPLY ONLY: SAUCE")
            # ``prompt`` is preserved as the user wrote it — only
            # the translator picks one over the other.
            self.assertEqual(a.prompt, "dummy")

    def test_instructions_inline_text_when_no_matching_file(self):
        """A value that doesn't match any sibling file is treated as inline.

        Matches the native Omnigent behavior — silent fall-through to
        inline avoids breaking specs whose authors typed an
        instruction that happens to look pathy.
        """
        yaml_content = "name: instr_agent\nprompt: dummy\ninstructions: this is just inline text\n"
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "agent.yaml"
            yaml_path.write_text(yaml_content)
            a = load_agent_def(str(yaml_path))
            self.assertEqual(a.instructions, "this is just inline text")

    def test_instructions_multiline_string_treated_as_inline(self):
        """Multi-line values can't be paths; treated as inline."""
        yaml_content = (
            "name: instr_agent\nprompt: dummy\ninstructions: |\n  line one\n  line two\n"
        )
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "agent.yaml"
            yaml_path.write_text(yaml_content)
            a = load_agent_def(str(yaml_path))
            self.assertIn("line one", a.instructions or "")
            self.assertIn("line two", a.instructions or "")

    def test_instructions_absent_yields_none(self):
        """No ``instructions:`` key in YAML → ``a.instructions is None``.

        Catches a regression where the loader mistakenly populates
        a default that overrides ``prompt:``.
        """
        yaml_content = "name: simple\nprompt: hi\n"
        with tempfile.TemporaryDirectory() as td:
            yaml_path = Path(td) / "agent.yaml"
            yaml_path.write_text(yaml_content)
            a = load_agent_def(str(yaml_path))
            self.assertIsNone(a.instructions)
            self.assertEqual(a.prompt, "hi")

    def test_instructions_resolves_relative_to_yaml_dir_not_cwd(self):
        """Path resolution anchors on the YAML's parent dir, not os.getcwd().

        A user can run ``omnigent run /elsewhere/agent.yaml`` from
        anywhere; the ``instructions: AGENTS.md`` reference must
        find ``/elsewhere/AGENTS.md``, not ``./AGENTS.md`` from
        the user's current working directory.
        """
        yaml_content = "name: instr_agent\nprompt: dummy\ninstructions: AGENTS.md\n"
        with tempfile.TemporaryDirectory() as outer:
            inner = Path(outer) / "agent_dir"
            inner.mkdir()
            yaml_path = inner / "agent.yaml"
            yaml_path.write_text(yaml_content)
            (inner / "AGENTS.md").write_text("FROM INNER DIR")
            # Decoy at the outer dir to make sure we're not just
            # finding the wrong AGENTS.md.
            (Path(outer) / "AGENTS.md").write_text("FROM OUTER DIR")

            original_cwd = os.getcwd()
            try:
                os.chdir(outer)  # cwd is OUTER but YAML is in INNER
                a = load_agent_def(str(yaml_path))
            finally:
                os.chdir(original_cwd)
            self.assertEqual(a.instructions, "FROM INNER DIR")

    def test_instructions_from_dict_input_treated_as_inline(self):
        """Loading from a raw dict has no path anchor → inline only.

        Tools that synthesize an AgentDef from a dict (rather than a
        file) shouldn't pretend to do path resolution; the
        ``instructions:`` value is whatever string the caller passed.
        """
        a = load_agent_def({"name": "x", "prompt": "p", "instructions": "AGENTS.md"})
        # No file resolution happens here; the value is the literal
        # string the caller provided.
        self.assertEqual(a.instructions, "AGENTS.md")


def test_instructions_rejects_path_traversal() -> None:
    """An out-of-bundle ``instructions:`` reference is treated as inline text.

    Mirrors the parser guard (W7 spec-injection): a YAML setting
    ``instructions: ../secret.txt`` must not make the loader read a file
    outside the YAML's own directory and fold it into the agent prompt. The
    value falls back to literal text so the file's contents never leak into
    ``instructions``. A regression here would surface the secret body.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "secret.txt").write_text("TOP SECRET RUNNER FILE")
        bundle = root / "bundle"
        bundle.mkdir()
        yaml_path = bundle / "agent.yaml"
        yaml_path.write_text("name: evil\nprompt: dummy\ninstructions: ../secret.txt\n")

        a = load_agent_def(str(yaml_path))

        # The out-of-root target is never read — its contents must not leak.
        assert "TOP SECRET" not in (a.instructions or "")
        # Falls back to the literal value (the existing inline-text path).
        assert a.instructions == "../secret.txt"


class TestLoaderOsEnvValidation(unittest.TestCase):
    """Validate that ``inner.loader`` mirrors Omnigent parser sandbox checks.

    The legacy ``load_agent_def`` is what the CLI ``omnigent run``
    actually invokes (via the omnigent-compat shim). If the legacy
    loader silently accepts a misconfigured sandbox (egress_rules on a
    non-enforcing backend, start_in_scratch without an active sandbox,
    etc.), the user gets a spec that **looks** hardened but isn't —
    the same class of bug as the recent terminal egress-rules drop.
    """

    def test_load_agent_def_preserves_egress_allow_private_destinations(self):
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    egress_rules:
      - "* api.github.com/repos/org/**"
    egress_allow_private_destinations: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                agent = load_agent_def(f.name)
            finally:
                os.unlink(f.name)
        self.assertIsNotNone(agent.os_env)
        sb = agent.os_env.sandbox
        self.assertIsNotNone(sb)
        self.assertEqual(
            sb.egress_rules,
            ["* api.github.com/repos/org/**"],
        )
        # Regression: loader used to silently drop this field.
        self.assertEqual(sb.egress_allow_private_destinations, True)

    def test_load_agent_def_rejects_egress_rules_on_none(self):
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: none
    egress_rules:
      - "* api.github.com/**"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(ValueError, "egress_rules requires"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_start_in_scratch_with_sandbox_none(self):
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  start_in_scratch: true
  sandbox:
    type: none
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(ValueError, "start_in_scratch requires"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_start_in_scratch_with_fork(self):
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  fork: true
  start_in_scratch: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_allow_sandbox_override_with_egress_rules(self):
        """A terminal that inherits egress_rules cannot also allow the
        LLM to override sandbox.type.

        An override to ``"none"`` cannot hard-enforce egress, so an
        override would leave ``egress_rules`` on the policy as inert
        decoration while the LLM bypassed the network allow-list.
        """
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    egress_rules:
      - "* api.github.com/**"
terminals:
  shell:
    command: zsh
    os_env: inherit
    allow_sandbox_override: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(
                    ValueError, "allow_sandbox_override.*incompatible.*egress_rules"
                ):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_allow_sandbox_override_with_own_egress_rules(self):
        """Same rule applies when the terminal carries its own egress_rules,
        not just when it inherits the agent's sandbox.
        """
        yaml_content = """
name: t
prompt: hi
terminals:
  shell:
    command: zsh
    allow_sandbox_override: true
    os_env:
      type: caller_process
      sandbox:
        type: darwin_seatbelt
        egress_rules:
          - "* api.github.com/**"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(
                    ValueError, "allow_sandbox_override.*incompatible.*egress_rules"
                ):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_non_bool_egress_allow_private_destinations(self):
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    egress_allow_private_destinations: "yes"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(TypeError, "must be a boolean"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_parses_credential_proxy(self):
        """Single-file omnigent YAML must parse ``credential_proxy``.

        Regression: this loader (the path ``omnigent run agent.yaml``
        takes, distinct from the bundle ``parse(config.yaml)`` path)
        had no ``credential_proxy`` parsing, so the field was silently
        dropped and the secretless proxy never armed even though the
        YAML declared it. We assert the entry actually reaches the spec
        with the right host/scheme/injection — not merely that the
        sandbox is non-None — because a dropped field would leave
        ``credential_proxy`` as ``None`` while everything else parsed.
        """
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    egress_rules:
      - "* corp.example.com/**"
    credential_proxy:
      - type: https_bearer
        target: corp.example.com/rest
        source: {env: CORP}
        env: CORP_TOKEN
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                agent = load_agent_def(f.name)
            finally:
                os.unlink(f.name)
        proxy = agent.os_env.sandbox.credential_proxy
        self.assertIsNotNone(proxy)
        # The YAML declares exactly one credential_proxy binding; a
        # different count would mean the loader dropped it (the original
        # bug) or duplicated it.
        self.assertEqual(len(proxy.entries), 1)
        entry = proxy.entries[0]
        self.assertEqual(entry.host, "corp.example.com")
        self.assertEqual(entry.scheme, "bearer")
        self.assertEqual(entry.inject_env, ["CORP_TOKEN"])
        self.assertEqual(entry.source.kind, "env")
        self.assertEqual(entry.source.env, "CORP")

    def test_load_agent_def_rejects_credential_proxy_without_egress_rules(self):
        """``credential_proxy`` without ``egress_rules`` is rejected here too.

        The MITM egress proxy (driven by egress_rules) is what performs
        the synthetic->real swap and blocks placeholder leaks; without it
        the proxy injects a placeholder the agent can't use. The loader
        must fail loud rather than hand back an inert, half-wired policy
        — mirroring the bundle parser guard so the two paths can't drift.
        """
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    credential_proxy:
      - type: git_https
        target: github.com
        source: {env: GH_PAT}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(ValueError, r"requires os_env\.sandbox\.egress_rules"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_credential_proxy_on_soft_backend(self):
        """``credential_proxy`` requires a network-isolating backend.

        On a soft backend (here ``none``) the egress proxy is not the
        only way out, so binding credentials to it is unsafe. We omit
        ``egress_rules`` so the backend guard (checked first) is the one
        that fires, isolating the credential_proxy-specific backend
        requirement rather than the generic egress-rules backend guard.
        """
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: none
    credential_proxy:
      - type: git_https
        target: github.com
        source: {env: GH_PAT}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(
                    ValueError, r"credential_proxy requires sandbox\.type"
                ):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)

    def test_load_agent_def_rejects_gh_basic_on_macos(self):
        """Single-file YAML rejects ``gh_basic`` on macOS too.

        ``gh_basic`` wires the GitHub CLI (a Go binary); Go on macOS ignores
        SSL_CERT_FILE and verifies TLS via the keychain, so it rejects the
        egress MITM CA and every ``gh`` call fails at runtime with
        ``certificate is not trusted``. The single-file loader (the
        ``omnigent run agent.yaml`` path) must fail loud at load time with the
        same explanation as the bundle parser — sharing one detection helper so
        the two paths can't drift.
        """
        yaml_content = """
name: t
prompt: hi
os_env:
  type: caller_process
  sandbox:
    type: darwin_seatbelt
    egress_rules:
      - "* github.com/**"
      - "* api.github.com/**"
    credential_proxy:
      - type: gh_basic
        source: {env: GH_PAT}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            try:
                with self.assertRaisesRegex(ValueError, r"gh_basic' does not work on macOS"):
                    load_agent_def(f.name)
            finally:
                os.unlink(f.name)


def test_factory_params_with_unresolvable_handler_does_not_crash() -> None:
    """factory_params + a handler that cannot be imported must not raise.

    Previously the loader passed ``factory_params`` to ``FunctionPolicy``
    even when ``_resolve_callable`` returned ``None`` (import error),
    which tripped ``FunctionPolicy.__post_init__``'s
    ``"factory_params requires factory"`` invariant check. The loader's
    contract is to silently handle missing callables — the error only
    surfaces when the policy is *evaluated*, not when the YAML is *loaded*.
    """
    agent = load_agent_def(
        {
            "name": "t",
            "policies": {
                "broken_policy": {
                    "type": "function",
                    "handler": "this.module.does.not.exist.fn",
                    "factory_params": {"limit": 5},
                }
            },
        }
    )
    # Must load without raising — the callable is None (import failed)
    # but the FunctionPolicy is constructed without factory_params so
    # __post_init__ does not raise.
    policy = agent.policies["broken_policy"]
    assert isinstance(policy, FunctionPolicy)
    assert policy.callable is None
    assert policy.factory is None
    # factory_params must be cleared when the factory could not be
    # resolved — keeping them with factory=None would re-trigger the
    # invariant on the next __copy__ or serialization path.
    assert policy.factory_params == {}


def test_load_agent_def_allows_custom_handler_by_default() -> None:
    """Trusted loading (the default) keeps supporting custom handlers.

    This is the operator/local ``omnigent run`` path — the custom
    FunctionPolicy feature. An unregistered, non-built-in handler must
    load without error when ``enforce_handler_allowlist`` is not set, so
    the bundle guard does not regress local custom policies.
    """
    agent = load_agent_def(
        {
            "name": "t",
            "policies": {
                "custom": {
                    "type": "function",
                    "handler": "my.org.custom_policy.rate_limit",
                    "factory_params": {"limit": 5},
                }
            },
        }
    )
    assert "custom" in agent.policies


def test_load_agent_def_enforce_rejects_unregistered_handler() -> None:
    """``enforce_handler_allowlist=True`` rejects an unregistered handler.

    This is the untrusted bundle-upload path. Rejection
    happens before ``_parse_agent_def`` resolves or calls the factory, so
    an uploaded ``subprocess.Popen`` policy never executes.
    """
    with pytest.raises(ValueError, match=r"not a registered policy handler"):
        load_agent_def(
            {
                "name": "t",
                "policies": {
                    "rce": {
                        "type": "function",
                        "handler": "subprocess.Popen",
                        "factory_params": {"args": ["id"]},
                    }
                },
            },
            enforce_handler_allowlist=True,
        )


def test_load_agent_def_enforce_allows_registered_handler() -> None:
    """``enforce_handler_allowlist=True`` still allows a registered handler.

    A built-in registry handler passes the upload allowlist, so the guard
    does not over-block legitimate bundles.
    """
    agent = load_agent_def(
        {
            "name": "t",
            "policies": {
                "ask_os": {
                    "type": "function",
                    "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
                }
            },
        },
        enforce_handler_allowlist=True,
    )
    assert "ask_os" in agent.policies


if __name__ == "__main__":
    unittest.main()
