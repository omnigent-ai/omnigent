"""
Unit tests for the Omnigent YAML spec adapter.

Covers:

- Forward-direction field-family translations (name+prompt,
  executor block, function-type tools).
- Fail-loud behavior on unsupported concepts (policies,
  ``os_env``, MCP tools, ``cancellable_function`` tools).
- Dispatch detection in :func:`omnigent.spec.load`:
  omnigent YAMLs route to the adapter; omnigent YAMLs
  (identified by ``spec_version``) use the existing parser.

These are the phase 2 translation unit tests + fail-loud tests
called out in ``designs/OMNIGENT_INTEGRATION.md`` under the
phase 2 test scope.

Round-trip tests live in ``test_omnigent_roundtrip.py`` since
they depend on phase 1's ``agent_spec_to_agent_def`` being
merged first.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec import load
from omnigent.spec.omnigent import (
    OMNIGENT_EXECUTOR_TYPE,
    OMNIGENT_TOOL_LANGUAGE,
    agent_def_to_agent_spec,
)
from omnigent.spec.types import AgentSpec

if TYPE_CHECKING:
    from omnigent.inner.datamodel import AgentDef

# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def hello_world_yaml(tmp_path: Path) -> Path:
    """
    Minimal omnigent YAML: ``name`` + ``prompt`` only.

    Matches ``examples/hello_world.yaml``. Used by detection,
    hello-world translation, and round-trip tests.
    """
    config = {
        "name": "hello_world",
        "prompt": "You are a friendly assistant. Say hello.",
    }
    path = tmp_path / "hello_world.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def executor_block_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with an ``executor:`` block declaring
    model, harness, and profile.
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
def function_tools_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with one function-type tool whose
    ``callable:`` points at a real, importable Python function
    (``tests.resources.examples._shared.tool_functions.get_current_time``). The adapter
    recovers the dotted path from the resolved callable's
    ``__module__`` + ``__qualname__``.
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


@pytest.fixture()
def policies_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML declaring a ``policies:`` block. The adapter
    lifts this into ``AgentSpec.guardrails.policies`` so the
    omnigent workflow enforces it at the configured phases.

    The YAML includes an ``executor:`` block so the synthesized
    AgentSpec passes the validator's harness-required check;
    policy translation is orthogonal to executor resolution.
    """
    config = {
        "name": "policy_example",
        "prompt": "I have policies.",
        "executor": {
            "model": "databricks-gpt-5-mini",
            "harness": "openai-agents",
        },
        "policies": {
            "block_foo": {
                "type": "function",
                "on": ["tool_call"],
                "handler": "tests.resources.examples._shared.tool_functions.block_long_sleep",
            },
        },
    }
    path = tmp_path / "policies.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def os_env_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML declaring a top-level ``os_env:`` block. The
    adapter carries it through the top-level ``AgentSpec.os_env`` field as an
    :class:`OSEnvSpec` dataclass so sub-agents that declare
    ``os_env: inherit`` can resolve to it at translation time.

    Needs a harness on the ``executor`` block so the synthesized
    AgentSpec passes the validator's harness-required check; the
    os_env translation itself is orthogonal to harness selection.
    """
    config = {
        "name": "os_env_example",
        "prompt": "I touch the filesystem.",
        "executor": {
            "model": "databricks-claude-sonnet-4",
            "harness": "claude-sdk",
        },
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
        },
    }
    path = tmp_path / "os_env.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def mcp_tool_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with a stdio MCP-type tool.

    Translated to an ``MCPServerConfig(transport="stdio",
    command=..., args=...)`` by the adapter — the
    subprocess is later srt-wrapped (when available) by
    :class:`~omnigent.tools.mcp.McpServerConnection`.

    Includes ``executor.harness`` so the synthesized AgentSpec
    passes :mod:`omnigent.spec.validator` — the adapter runs
    validation after translation and bails loud on missing
    required fields.
    """
    config = {
        "name": "mcp_example",
        "prompt": "I use MCP.",
        "executor": {"harness": "claude-sdk", "model": "databricks-claude-sonnet-4"},
        "tools": {
            "glean": {
                "type": "mcp",
                "command": ".venv/bin/python",
                "args": ["-m", "omnigent.inner.databricks_mcps.glean"],
            },
        },
    }
    path = tmp_path / "mcp.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def mcp_http_tool_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with an HTTP MCP-type tool (``url`` + headers).

    Translated to an ``MCPServerConfig(transport="http", url=...,
    headers=...)`` by the adapter. Covers the non-stdio
    branch of :func:`_translate_mcp_tool_from_def`.
    """
    config = {
        "name": "mcp_http_example",
        "prompt": "I use HTTP MCP.",
        "executor": {"harness": "claude-sdk"},
        "tools": {
            "github": {
                "type": "mcp",
                "url": "https://mcp.example.com/sse",
                "headers": {"Authorization": "Bearer tok_xyz"},
            },
        },
    }
    path = tmp_path / "mcp_http.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def mcp_databricks_server_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML with the ``databricks_server`` MCP shape —
    omnigent has no resolver for it, so the adapter rejects.
    """
    config = {
        "name": "mcp_db_example",
        "prompt": "I use a named Databricks MCP.",
        "executor": {"harness": "claude-sdk"},
        "tools": {
            "uc": {
                "type": "mcp",
                "databricks_server": "unity-catalog",
                "profile": "test-profile",
            },
        },
    }
    path = tmp_path / "mcp_db.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def cancellable_tool_yaml(tmp_path: Path) -> Path:
    """
    Omnigent YAML declaring a legacy ``cancellable_function``
    tool. Used to verify the adapter REJECTS this shape post-step
    (c) — the runner protocol was retired in favor of plain
    callables dispatched via ``sys_call_async``.
    """
    config = {
        "name": "cancellable_example",
        "prompt": "I can sleep.",
        "executor": {
            "model": "databricks-claude-sonnet-4",
            "harness": "claude-sdk",
        },
        "tools": {
            "sleep": {
                "type": "cancellable_function",
                "runner": "tests.resources.examples._shared.tool_functions.sleep_tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number"},
                    },
                    "required": ["seconds"],
                },
            },
        },
    }
    path = tmp_path / "cancellable.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture()
def omnigent_spec_dir(tmp_path: Path) -> Path:
    """
    An omnigent spec directory (``spec_version: 1`` in
    ``config.yaml``). Routes through the existing parser, not
    the omnigent adapter. Used by the detection tests.
    """
    config = {
        "spec_version": 1,
        "name": "ap-agent",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


# ── Translation: direct agent_def_to_agent_spec ──────────────


def test_agent_def_to_agent_spec_hello_world(
    hello_world_yaml: Path,
) -> None:
    """
    A minimal YAML (name + prompt only) translates to an
    AgentSpec with name, instructions, spec_version=1, and
    executor.type='omnigent'.

    What breaks if this fails: the baseline phase 2 dispatch —
    ``omnigent chat hello_world.yaml`` can't produce a valid spec
    without this path working.
    """
    from omnigent.inner.loader import load_agent_def

    agent_def = load_agent_def(hello_world_yaml)
    spec = agent_def_to_agent_spec(agent_def)

    assert isinstance(spec, AgentSpec)
    assert spec.name == "hello_world"
    # prompt → instructions verbatim.
    assert spec.instructions == "You are a friendly assistant. Say hello."
    # spec_version is synthesized to the current omnigent
    # schema version (no spec_version in omnigent YAMLs).
    assert spec.spec_version == 1
    # executor.type drives the runtime to pick OmnigentExecutor.
    assert spec.executor.type == OMNIGENT_EXECUTOR_TYPE
    # Hello world has no executor block, no llm, no tools.
    assert spec.llm is None
    assert spec.local_tools == []


def test_agent_def_to_agent_spec_accepts_claude_harness_alias(tmp_path: Path) -> None:
    """Omnigent YAML may use ``harness: claude`` as a spec-level alias."""
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "alias_agent",
                "prompt": "hi",
                "executor": {
                    "model": "databricks-claude-sonnet-4",
                    "harness": "claude",
                },
            }
        )
    )

    spec = load(yaml_path)

    assert spec.executor.type == OMNIGENT_EXECUTOR_TYPE
    assert spec.executor.config["harness"] == "claude-sdk"


def test_agent_def_to_agent_spec_executor_block(
    executor_block_yaml: Path,
) -> None:
    """
    An ``executor:`` block with model + harness + profile
    populates :attr:`LLMConfig.model` and the harness/profile
    entries in :attr:`ExecutorSpec.config` (the shared
    round-trip contract with phase 1's
    ``agent_spec_to_agent_def``).

    What breaks if this fails: the OmnigentExecutor cannot
    pick a harness or workspace profile at instantiation time,
    so every non-trivial omnigent YAML routes to the wrong
    harness (or fails).
    """
    from omnigent.inner.loader import load_agent_def

    agent_def = load_agent_def(executor_block_yaml)
    spec = agent_def_to_agent_spec(agent_def)

    assert spec.llm is not None
    assert spec.llm.model == "databricks-claude-sonnet-4"
    assert spec.executor.type == OMNIGENT_EXECUTOR_TYPE
    # harness / profile land in executor.config (typed dict),
    # NOT setattr-added attributes. This is the shared wire
    # contract with phase 1's agent_spec_to_agent_def.
    assert spec.executor.config["harness"] == "claude-sdk"
    assert spec.executor.config["profile"] == "test-profile"
    # Mirrored to top-level too: supervisor spawn-env reads
    # spec.executor.profile, not config["profile"].
    assert spec.executor.profile == "test-profile"


def test_agent_def_to_agent_spec_unknown_model_raises(
    tmp_path: Path,
) -> None:
    """
    A YAML with a model that has no known harness prefix raises an
    error — every agent must resolve to a named harness.

    :param tmp_path: Pytest-provided temporary directory.
    """
    yaml_path = tmp_path / "kimi.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "kimi",
                "prompt": "You are Kimi.",
                "executor": {"model": "databricks/databricks-kimi-k2-6"},
            }
        )
    )

    with pytest.raises(Exception, match=r"[Hh]arness"):
        load(yaml_path)


def test_agent_def_to_agent_spec_function_tool(
    function_tools_yaml: Path,
) -> None:
    """
    A function-type tool with ``callable:
    tests.resources.examples._shared.tool_functions.get_current_time`` translates to a
    :class:`LocalToolInfo` whose ``path`` is the recovered
    dotted module path.

    What breaks if this fails: OmnigentExecutor cannot resolve
    the tool callable on the reverse trip, so the harness starts
    without its tools.
    """
    from omnigent.inner.loader import load_agent_def

    agent_def = load_agent_def(function_tools_yaml)
    spec = agent_def_to_agent_spec(agent_def)

    assert len(spec.local_tools) == 1
    tool = spec.local_tools[0]
    assert tool.name == "get_current_time"
    # The dotted callable path is recovered from the resolved
    # callable's __module__ + __qualname__ (see
    # _recover_callable_path).
    assert tool.path == "tests.resources.examples._shared.tool_functions.get_current_time"
    # The language sentinel is how the forward direction
    # (agent_spec_to_agent_def, phase 1) knows this tool came
    # from an omnigent YAML and must be re-resolved via
    # importlib.import_module rather than read off disk.
    assert tool.language == OMNIGENT_TOOL_LANGUAGE


def test_agent_def_to_agent_spec_translates_catalog_path_tool(tmp_path: Path) -> None:
    """
    ``catalog_path`` Unity Catalog tools translate into
    ``LocalToolInfo`` with ``runtime=UC_FUNCTION``.

    Failure meaning: UC tools are silently dropped or rejected
    instead of being carried through to the runner for execution
    via the SQL Statement Execution API.

    :param tmp_path: Pytest temporary directory for the YAML fixture.
    """
    from omnigent.inner.loader import load_agent_def
    from omnigent.spec.types import ToolRuntime

    yaml_path = tmp_path / "uc.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "uc_agent",
                "prompt": "Use UC functions.",
                "executor": {"model": "databricks-claude-sonnet-4"},
                "tools": {
                    "classify": {
                        "type": "function",
                        "catalog_path": "main.default.classify",
                        "warehouse_id": "wh-abc",
                    },
                },
            },
        ),
    )

    agent_def = load_agent_def(yaml_path)
    spec = agent_def_to_agent_spec(agent_def)

    # UC tool translated into a LocalToolInfo with UC_FUNCTION runtime.
    uc_tools = [t for t in spec.local_tools if t.catalog_path is not None]
    assert len(uc_tools) == 1, (
        f"Expected exactly 1 UC tool, got {len(uc_tools)}. "
        f"If 0, the UC tool was silently dropped during translation."
    )
    tool = uc_tools[0]
    assert tool.name == "classify"
    assert tool.catalog_path == "main.default.classify"
    assert tool.warehouse_id == "wh-abc"
    assert tool.runtime == ToolRuntime.UC_FUNCTION
    # path is None for UC tools (no server-side callable).
    assert tool.path is None


def test_function_tool_parameters_derived_from_callable_signature(
    function_tools_yaml: Path,
) -> None:
    """
    When the YAML's function tool declares no ``input_schema:``,
    the Omnigent adapter introspects the resolved Python callable's
    signature and exposes that as the LLM-facing JSON-Schema
    ``parameters`` block. Without this fallback, omnigent
    YAMLs that point at plain Python functions ship to the LLM
    with empty parameters and the model invokes the tool with
    zero arguments — surfacing as
    ``TypeError: <fn>() missing 1 required positional argument``
    when the harness dispatches the call.

    The reference YAML's ``get_current_time`` callable signature
    is ``(timezone_name: str = "UTC") -> str``: optional string
    parameter. Verify the schema reflects that exactly.

    What breaks if this fails:
      - The adapter reverts to forwarding only an explicit
        ``input_schema:`` block from YAML (the prior buggy
        behaviour); plain Python tools become unusable under
        Omnigent mode.
      - The schema-derivation helper changes its output shape —
        e.g. drops ``required`` for keyword-only-with-default
        params, or starts emitting required entries for
        defaulted ones.
    """
    from omnigent.inner.loader import load_agent_def

    agent_def = load_agent_def(function_tools_yaml)
    spec = agent_def_to_agent_spec(agent_def)

    tool = spec.local_tools[0]
    assert tool.name == "get_current_time"
    # The fallback runs ``_schema_from_callable`` against
    # ``get_current_time``'s ``(timezone_name: str = "UTC")``
    # signature. ``timezone_name`` has a default → not required,
    # but still must appear under ``properties``.
    assert tool.parameters is not None, (
        "parameters must be populated from the callable signature; "
        "if None, the LLM gets zero-arg tool stubs and calls them "
        "with no arguments at runtime (TypeError at dispatch)."
    )
    assert tool.parameters.get("type") == "object"
    properties = tool.parameters.get("properties", {})
    assert "timezone_name" in properties, (
        f"signature param 'timezone_name' missing from derived schema; "
        f"got properties={properties!r}"
    )
    assert properties["timezone_name"] == {"type": "string"}
    # Defaulted param → not required.
    required = tool.parameters.get("required", [])
    assert "timezone_name" not in required, (
        f"defaulted param leaked into 'required'; got {required!r}"
    )


# ── Fail-loud: each unsupported concept gets its own test ─────


def test_load_omnigent_yaml_missing_package_raises_with_install_hint(
    hello_world_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the ``omnigent`` package is not importable (e.g. agent-
    plane pip-installed standalone without the sibling omnigent
    source on PYTHONPATH), loading an omnigent YAML must surface
    a friendly :class:`OmnigentError` with an install hint —
    not a bare ``ModuleNotFoundError`` from deep in the import
    machinery.

    What breaks if this fails: a user running ``omnigent chat foo.yaml``
    from an env that only has omnigent gets a cryptic
    ``ModuleNotFoundError: No module named 'omnigent'`` from
    ``_omnigent_compat.py``'s import line, with no clue what
    to install. The rewritten error says what's missing and how
    to fix it.
    """
    # Simulate ``omnigent`` not being importable by wiping it
    # from ``sys.modules`` and blocking fresh imports. Using a
    # meta-path finder (vs ``sys.modules[...] = None``) catches
    # both cold imports AND any submodule-level import the compat
    # shim might try.
    import sys

    for mod_name in list(sys.modules):
        if mod_name == "omnigent" or mod_name.startswith("omnigent."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

    class _OmnigentBlocker:
        """Meta-path finder that refuses to resolve omnigent."""

        def find_spec(
            self,
            fullname: str,
            path: Sequence[str] | None,
            target: ModuleType | None = None,
        ) -> ModuleSpec | None:
            del path, target
            if fullname == "omnigent" or fullname.startswith("omnigent."):
                # Pretend the package doesn't exist at all.
                raise ModuleNotFoundError(
                    f"No module named {fullname!r}",
                    name=fullname,
                )
            return None

    blocker = _OmnigentBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])

    with pytest.raises(OmnigentError) as exc_info:
        load(hello_world_yaml)

    # Error message points at the missing package by name AND
    # gives an actionable install instruction. A bare
    # ModuleNotFoundError would say neither.
    message = str(exc_info.value)
    assert "omnigent" in message
    assert "pip install" in message, (
        f"Expected an install hint in the error message; got: {message!r}"
    )


def test_load_policies_yaml_lifts_into_guardrails(policies_yaml: Path) -> None:
    """
    Omnigent YAMLs with a ``policies:`` block produce an
    AgentSpec whose ``guardrails.policies`` carries the
    translated policy, preserving ``name``, the dotted callable
    path, and the phase. The omnigent workflow then enforces
    at runtime via the standard :class:`PolicyEngine`.

    What breaks if this fails: policies in an omnigent YAML
    either silently disappear (unsafe — agent runs without the
    author's declared guardrails) or fail spec-load (regression
    to the pre-lift rejection path).
    """
    from omnigent.spec.types import FunctionPolicySpec

    spec = load(policies_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.policies is not None
    # Exactly one policy survived — anything else would mean the
    # translator accidentally duplicated or dropped.
    assert len(spec.guardrails.policies) == 1
    policy = spec.guardrails.policies[0]
    assert isinstance(policy, FunctionPolicySpec)
    assert policy.name == "block_foo"
    # on: is ignored for function policies — callable self-selects.
    assert policy.on is None
    # The author's dotted callable path travels under the shim's
    # ``target`` argument so legacy ``(content, phase)`` callables
    # get adapted at policy-build time.
    assert policy.function is not None
    assert policy.function.path == "omnigent.spec._omnigent_legacy_shim.build"
    assert policy.function.arguments == {
        "target": "tests.resources.examples._shared.tool_functions.block_long_sleep",
    }


def test_load_os_env_yaml_carries_through_top_level_field(
    os_env_yaml: Path,
) -> None:
    """
    A top-level ``os_env:`` block on an omnigent YAML
    translates into an :class:`OSEnvSpec` dataclass stashed on
    ``AgentSpec.os_env`` (the native top-level field). The
    dataclass flows by reference — no hand-rolled dict
    serialization — because ``AgentSpec`` is never persisted
    to disk on this path.

    What breaks if this fails: the adapter either regresses to
    the old fail-loud (rejecting every YAML with an os_env
    block), or the ``inherit`` sentinel on inline-AgentTool
    sub-agents has no concrete parent to resolve against and
    sub-agents boot without filesystem access.
    """
    from omnigent.inner.datamodel import OSEnvSpec

    spec = load(os_env_yaml)

    # The dataclass itself is what the adapter carries — a plain
    # dict would mean the serializer re-appeared and the round
    # trip bakes in copy-overhead we don't need.
    assert isinstance(spec.os_env, OSEnvSpec), (
        f"spec.os_env must hold the OSEnvSpec dataclass, got "
        f"{type(spec.os_env).__name__!r}. Hand-rolled dict "
        f"serialization is the antipattern we're avoiding."
    )
    assert spec.os_env.type == "caller_process"
    assert spec.os_env.cwd == "."
    # The legacy executor.config["os_env"] storage is gone; the
    # field flows on the top-level ``AgentSpec.os_env`` only.
    assert "os_env" not in spec.executor.config


def test_load_mcp_stdio_yaml_translates_to_mcp_server(mcp_tool_yaml: Path) -> None:
    """
    Omnigent YAMLs declaring a subprocess MCP tool translate to
    a native ``MCPServerConfig(transport="stdio", ...)`` entry on
    ``AgentSpec.mcp_servers``. At runtime
    :class:`~omnigent.tools.mcp.McpServerConnection` spawns the
    subprocess, srt-wrapped when available.

    What breaks if this fails: the adapter regresses to the
    old fail-loud rejection, making agents with MCPs
    (e.g. databricks_coding_agent's glean/google) unusable
    under the Omnigent integration path.
    """
    spec = load(mcp_tool_yaml)
    assert len(spec.mcp_servers) == 1
    mcp = spec.mcp_servers[0]
    # Identity fields come from the YAML key + tool body.
    assert mcp.name == "glean"
    assert mcp.transport == "stdio"
    # Command + args carry through verbatim so the subprocess
    # spawn matches what legacy omnigent ran.
    assert mcp.command == ".venv/bin/python"
    assert mcp.args == ["-m", "omnigent.inner.databricks_mcps.glean"]
    # HTTP fields must stay None / empty on the stdio branch.
    assert mcp.url is None
    assert mcp.headers == {}


def test_load_mcp_http_yaml_translates_to_mcp_server(mcp_http_tool_yaml: Path) -> None:
    """
    Omnigent YAMLs with an HTTP MCP (``url`` + headers)
    translate to an ``MCPServerConfig(transport="http", ...)``
    entry. Covers the non-stdio branch of
    :func:`_translate_mcp_tool_from_def`.

    What breaks if this fails: users migrating HTTP MCPs from
    omnigent-legacy to Omnigent mode get either a translator crash
    (``None`` command) or a silently dropped tool.
    """
    spec = load(mcp_http_tool_yaml)
    assert len(spec.mcp_servers) == 1
    mcp = spec.mcp_servers[0]
    assert mcp.name == "github"
    assert mcp.transport == "http"
    assert mcp.url == "https://mcp.example.com/sse"
    # Headers carry through — provider-auth headers like
    # Authorization must survive translation.
    assert mcp.headers == {"Authorization": "Bearer tok_xyz"}
    # Stdio fields stay empty on the http branch.
    assert mcp.command is None
    assert mcp.args == []
    assert mcp.env == {}


def test_mcp_stdio_yaml_reverse_trip_recovers_mcp_tool(mcp_tool_yaml: Path) -> None:
    """
    Forward + reverse round-trip: YAML → AgentSpec (with
    MCPServerConfig) → AgentDef (with MCPTool). The reverse
    path is what :meth:`OmnigentExecutor.from_spec` calls
    when wrapping an omnigent spec for an omnigent
    harness; a missing reverse translation drops every MCP
    tool from the AgentDef the harness sees.

    What breaks if this fails: a live Omnigent mode run with a
    stdio MCP either crashes (reverse path raises
    ``unsupported concept``) or silently drops the MCP tool
    (LLM sees no MCP tool, never calls it, agent returns
    "I don't have that tool"). Covers the exact regression
    the live E2E test under tests/e2e/omnigent/ guards
    against.
    """
    from omnigent.inner.tools import MCPTool
    from omnigent.spec.omnigent import agent_spec_to_agent_def

    spec = load(mcp_tool_yaml)
    agent_def = agent_spec_to_agent_def(spec)
    tool = agent_def.tools.get("glean")
    assert isinstance(tool, MCPTool), (
        f"Expected reverse trip to recover an MCPTool under 'glean'; "
        f"got {type(tool).__name__!r}. If None, the reverse translator "
        f"dropped the MCP server silently."
    )
    # Transport fields round-trip: command + args must match the
    # originally-declared subprocess, not some lossy approximation.
    assert tool.command == ".venv/bin/python"
    assert tool.args == ["-m", "omnigent.inner.databricks_mcps.glean"]


def test_load_mcp_databricks_server_yaml_raises(mcp_databricks_server_yaml: Path) -> None:
    """
    Omnigent MCP tools using the ``databricks_server=<name>``
    shape fail loud — Omnigent' MCPServerConfig doesn't
    resolve named Databricks servers. The translator needs a
    concrete ``url`` or ``command`` to emit a functional config.

    What breaks if this fails: specs with
    ``databricks_server: unity-catalog`` would silently translate
    to an MCPServerConfig with neither url nor command — the
    validator would then reject the spec at load, but with a
    less-helpful message than the pinpoint fail here.
    """
    with pytest.raises(OmnigentError, match="databricks_server"):
        load(mcp_databricks_server_yaml)


def test_load_cancellable_function_yaml_rejected_post_step_c(
    cancellable_tool_yaml: Path,
) -> None:
    """
    Omnigent YAMLs declaring ``type: cancellable_function``
    are rejected by the Omnigent adapter with a clear migration hint.

    Step (c) retired the runner-protocol shape (``runner:`` +
    ``CancellableFunctionTool``) in favor of plain callables
    dispatched via ``sys_call_async``. The adapter fails loud
    rather than silently translating, so anyone porting an old
    inner-stack YAML to Omnigent mode gets pointed at the new shape.

    What breaks if this fails: either the adapter regresses to
    silently accept runner instances (the bug that motivated
    step (c) — non-callable runner instances tripped the
    LocalCallableTool loader at runtime), or the migration
    hint disappears and users hit a confusing internal
    ``TypeError`` instead.
    """
    with pytest.raises(OmnigentError, match="cancellable_function"):
        load(cancellable_tool_yaml)


# ── Dispatch detection in omnigent.spec.load ──────────────


def test_load_omnigent_yaml_routes_to_adapter(
    executor_block_yaml: Path,
) -> None:
    """
    A ``.yaml`` file with ``name`` + ``prompt`` and no
    ``spec_version`` routes through the omnigent adapter.
    The produced spec has ``executor.type='omnigent'``, which
    no omnigent native spec ever emits.

    Uses ``executor_block_yaml`` (which declares a harness) rather
    than the bare ``hello_world_yaml`` so the synthesized spec
    passes the validator's harness-required check; the dispatch
    itself doesn't depend on which valid omnigent YAML we feed.

    What breaks if this fails: ``omnigent chat foo.yaml`` against an
    omnigent YAML would either crash (no config.yaml in a
    file-shaped source) or silently parse as an omnigent
    spec, producing nonsense.
    """
    spec = load(executor_block_yaml)
    assert spec.executor.type == OMNIGENT_EXECUTOR_TYPE
    assert spec.name == "executor_example"
    assert spec.executor.config["harness"] == "claude-sdk"


def test_load_omnigent_directory_uses_existing_parser(
    omnigent_spec_dir: Path,
) -> None:
    """
    An omnigent spec directory (``spec_version`` declared)
    routes through the existing parser unchanged. The resulting
    spec has ``executor.type='omnigent'`` (the default)
    and no omnigent extras.

    What breaks if this fails: all existing omnigent specs
    would stop loading (regression surfaces in every omnigent
    test suite, so this is a smoke check).
    """
    spec = load(omnigent_spec_dir)
    # Default executor type, proving the dispatch routed correctly.
    assert spec.executor.type == "omnigent"
    assert spec.name == "ap-agent"


def test_load_yaml_with_spec_version_not_routed_to_adapter(
    tmp_path: Path,
) -> None:
    """
    A ``.yaml`` file that happens to contain ``name`` +
    ``prompt`` but also declares ``spec_version`` is treated as
    an omnigent spec. The detection rule's negative check on
    ``spec_version`` prevents misrouting.

    This case currently still fails (omnigent YAML specs are
    directories, not files), but the dispatch MUST pick the
    non-omnigent branch so the resulting error is an
    omnigent "dest is required" / parser error — NOT an
    omnigent adapter error. The assertion below verifies the
    error shape is NOT an omnigent-adapter error.

    What breaks if this fails: a future change that starts
    supporting omnigent YAML-file specs would silently route
    to the wrong adapter.
    """
    config = {
        "spec_version": 1,
        "name": "hybrid",
        "prompt": "Looks omnigent-y but is omnigent.",
    }
    path = tmp_path / "hybrid.yaml"
    path.write_text(yaml.dump(config))

    # ``spec_version`` marks this file as an omnigent spec, but
    # omnigent specs must live in a directory with a
    # ``config.yaml``, not a single YAML file. ``load()`` rejects with
    # an actionable diagnostic. If detection were wrong, we'd get an
    # omnigent-adapter error (e.g. "missing system-prompt key")
    # instead — the assertion below pins the omnigent shape.
    with pytest.raises(OmnigentError, match="spec_version"):
        load(path)


# ── os_env propagation ───────────────────────────────────────


def test_cancellable_function_parameters_forward_trip_preserves_input_schema() -> None:
    """
    Forward: a :class:`CancellableFunctionTool` is rejected with
    a clear migration message — the runner protocol was retired
    in step (c) in favor of plain callables dispatched via
    ``sys_call_async``.

    **What breaks if this fails**: the adapter silently translates
    runner-protocol tools (the bug that motivated step (c) — the
    instance is non-callable, so ``LocalCallableTool`` trips at
    runtime with a confusing ``TypeError``). The fail-loud here
    catches the regression at translation time with an actionable
    message.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.inner.tools import CancellableFunctionTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    seconds_schema = {
        "type": "object",
        "properties": {"seconds": {"type": "number"}},
        "required": ["seconds"],
    }
    original = AgentDef(
        name="round_tripper",
        prompt="p",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        tools={
            "sleep": CancellableFunctionTool(
                name="sleep",
                description="Sleep for N seconds",
                runner=_stub_runner_instance,
                input_schema=seconds_schema,
            ),
        },
    )

    with pytest.raises(OmnigentError, match="cancellable_function"):
        agent_def_to_agent_spec(original)


def test_function_tool_parameters_round_trip_preserves_input_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Forward then reverse: a plain :class:`FunctionTool` with an
    explicit ``input_schema`` round-trips through omnigent
    translation and back without losing the schema.

    Step (c) made plain callables the only supported function-tool
    shape on the Omnigent path. Schema preservation matters because the
    inner harness's ``tool_schema()`` falls back to introspecting
    the callable when ``input_schema`` is absent — fine for
    well-typed functions, but fragile for tools with non-trivial
    parameter shapes (Pydantic models, optional fields, etc.).
    Pinning the round-trip catches regressions where the
    translator drops ``parameters`` somewhere along the way.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.inner.tools import FunctionTool
    from omnigent.spec import omnigent as spec_omni
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    seconds_schema = {
        "type": "object",
        "properties": {"seconds": {"type": "number"}},
        "required": ["seconds"],
    }

    def _stub_callable(seconds: float) -> dict[str, float]:
        """Stub used as the resolved callable in the reverse trip."""
        return {"slept": seconds}

    original = AgentDef(
        name="round_tripper",
        prompt="p",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        tools={
            "sleep": FunctionTool(
                name="sleep",
                description="Sleep for N seconds",
                callable=_stub_callable,
                input_schema=seconds_schema,
            ),
        },
    )

    spec = agent_def_to_agent_spec(original)
    assert len(spec.local_tools) == 1
    tool_info = spec.local_tools[0]
    assert tool_info.name == "sleep"
    assert tool_info.parameters == seconds_schema, (
        "FunctionTool lost its input_schema on the forward trip — "
        "the reverse trip will rebuild a no-args tool and the LLM will emit "
        "empty-argument tool calls."
    )

    # Reverse trip — stub the dotted-path resolver since
    # ``_stub_callable`` is a closure (not module-level) and
    # `_recover_callable_path` would have used the real qualname.
    monkeypatch.setattr(
        spec_omni,
        "_resolve_dotted_attr",
        lambda _path, _name: _stub_callable,
    )
    rebuilt = spec_omni.agent_spec_to_agent_def(spec)
    assert "sleep" in rebuilt.tools
    rebuilt_tool = rebuilt.tools["sleep"]
    assert rebuilt_tool.input_schema == seconds_schema
    advertised = rebuilt_tool.tool_schema()
    assert advertised.get("parameters") == seconds_schema


class _StubCancellableRunner:
    """
    Module-level runner class kept for the rejection test.

    Used solely to construct a :class:`CancellableFunctionTool`
    that the forward translator can reject. Never called.
    """

    def start(self, args: dict[str, Any], on_complete: Any) -> None:
        """Stub — never actually called by the tests above."""
        raise NotImplementedError


# Module-level binding so a stable instance exists for the
# rejection test's CancellableFunctionTool construction.
_stub_runner_instance = _StubCancellableRunner()


def test_os_env_round_trips_through_translator() -> None:
    """
    ``AgentDef`` → ``AgentSpec`` → ``AgentDef`` preserves the
    :class:`OSEnvSpec` dataclass by reference through the
    top-level ``AgentSpec.os_env`` field.

    What breaks if this fails: the forward/reverse translator
    stops round-tripping os_env, so agents that declared a
    top-level ``os_env:`` either lose it (sub-agents boot
    without FS access) or crash (hand-rolled dict conversion
    reintroduced and lost a field).
    """
    from omnigent.inner.datamodel import (
        AgentDef,
        OSEnvSandboxSpec,
        OSEnvSpec,
    )
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.spec.omnigent import (
        agent_def_to_agent_spec,
        agent_spec_to_agent_def,
    )

    original_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=["."],
            allow_network=False,
        ),
    )
    original = AgentDef(
        name="os_user",
        prompt="p",
        tools={},
        executor=OmniExecutorSpec(
            model="databricks-claude-sonnet-4",
            harness="claude-sdk",
            profile="test-profile",
        ),
        os_env=original_os_env,
    )

    spec = agent_def_to_agent_spec(original)
    # Reverse trip stores the dataclass by reference on the
    # top-level field — not in executor.config.
    assert spec.os_env is original_os_env
    assert "os_env" not in spec.executor.config

    forward = agent_spec_to_agent_def(spec)
    # Forward trip reads it back unchanged.
    assert forward.os_env is original_os_env


def test_inline_agent_tool_inherit_resolves_to_parent_os_env() -> None:
    """
    An inline :class:`AgentTool` that declares
    ``os_env: "inherit"`` picks up the parent's concrete
    :class:`OSEnvSpec` at translation time — omnigent spawns
    each sub-agent as an independent task with no live parent
    to consult at runtime.

    What breaks if this fails: ``coding_supervisor.yaml``-style
    sub-agents (``claude_worker: os_env: inherit``) boot with
    no OS environment and can't run shell/file tools against
    the repo. The whole point of ``os_env: inherit`` — matching
    legacy omnigent semantics — silently breaks.
    """
    from omnigent.inner.datamodel import (
        AgentDef,
        OSEnvSpec,
    )
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent_os_env = OSEnvSpec(type="caller_process", cwd=".")
    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
            profile="test-profile",
        ),
        os_env=parent_os_env,
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(
                    model="databricks-claude-opus-4",
                    harness="claude-sdk",
                ),
                # The sentinel the loader produces for
                # ``os_env: inherit`` in YAML.
                os_env="inherit",
            ),
        },
    )

    spec = agent_def_to_agent_spec(parent)

    # Parent still carries the original os_env by reference on
    # the top-level field.
    assert spec.os_env is parent_os_env
    # The single sub-agent inherited it — ``inherit`` resolved
    # at translation time.
    assert len(spec.sub_agents) == 1
    sub = spec.sub_agents[0]
    assert sub.os_env is parent_os_env, (
        f"Sub-agent's os_env should be the parent's OSEnvSpec (by reference); got {sub.os_env!r}"
    )


def test_inline_agent_tool_concrete_os_env_not_overridden_by_parent() -> None:
    """
    An inline AgentTool that declares its own concrete
    :class:`OSEnvSpec` is preserved — the ``inherit`` fallback
    only fires when the tool uses the string sentinel. Explicit
    always wins, same as the ``profile`` propagation rule.
    """
    from omnigent.inner.datamodel import (
        AgentDef,
        OSEnvSpec,
    )
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent_os_env = OSEnvSpec(type="caller_process", cwd=".")
    child_os_env = OSEnvSpec(type="caller_process", cwd="/tmp/sandbox")
    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        os_env=parent_os_env,
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(harness="claude-sdk"),
                os_env=child_os_env,
            ),
        },
    )

    spec = agent_def_to_agent_spec(parent)

    sub = spec.sub_agents[0]
    # The sub-agent's own os_env wins — parent's is NOT used.
    assert sub.os_env is child_os_env
    assert sub.os_env is not parent_os_env


def test_inline_agent_tool_inherit_with_no_parent_os_env_yields_none() -> None:
    """
    ``os_env: inherit`` with no parent os_env resolves to
    ``None`` — matches legacy omnigent behavior when the
    parent itself declares nothing. The sub-spec's
    ``executor.config`` omits the ``os_env`` key entirely so
    the forward trip rebuilds an ``AgentDef`` with
    ``os_env=None`` (no FS access — same as the
    commented-out ``coding_supervisor.yaml`` state the user
    experienced before this feature landed).
    """
    from omnigent.inner.datamodel import (
        AgentDef,
    )
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        # No os_env on the parent.
        os_env=None,
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(harness="claude-sdk"),
                os_env="inherit",
            ),
        },
    )

    spec = agent_def_to_agent_spec(parent)

    sub = spec.sub_agents[0]
    assert sub.os_env is None, (
        "An inline ``os_env: inherit`` with no parent os_env "
        "should leave the sub-spec's ``os_env`` field as "
        "``None``, not with a placeholder."
    )


# ── instructions: field (cross-format parity) ────────────────


def test_instructions_field_resolved_path_wins_over_prompt() -> None:
    """
    When an omnigent YAML declares both ``prompt:`` and
    ``instructions: <path>``, the resolved instructions content
    wins on the AgentSpec. Translator precedence rule from
    omnigent/spec/omnigent.py.

    What breaks if this fails: a user who writes
    ``instructions: AGENTS.md`` to point at a long external
    spec, plus a placeholder ``prompt: dummy`` for backwards
    compat, ends up with the placeholder instead of the real
    instructions.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = AgentDef(
        name="instr-precedence",
        prompt="placeholder",
        instructions="REAL FROM FILE",
    )
    spec = agent_def_to_agent_spec(agent_def)
    assert spec.instructions == "REAL FROM FILE"


def test_instructions_field_falls_back_to_prompt_when_unset() -> None:
    """
    When ``instructions:`` is absent (None), the translator falls
    back to ``prompt:`` — preserves backward compat for every
    omnigent YAML written before the field existed.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = AgentDef(name="prompt-only", prompt="just the prompt")
    spec = agent_def_to_agent_spec(agent_def)
    assert spec.instructions == "just the prompt"


def test_instructions_yaml_loads_through_full_pipeline(tmp_path: Path) -> None:
    """
    End-to-end through ``load_omnigent_yaml`` (the integration
    path the Omnigent server hits when registering an omnigent
    bundle): YAML with ``instructions: AGENTS.md`` produces a
    spec whose ``instructions`` field carries the file's
    contents.
    """
    from omnigent.spec._omnigent_compat import load_omnigent_yaml

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        "name: full_pipeline\n"
        "prompt: dummy placeholder\n"
        "instructions: AGENTS.md\n"
        "executor:\n"
        "  harness: openai-agents\n"
        "  model: gpt-4o\n"
    )
    (tmp_path / "AGENTS.md").write_text("FROM AGENTS DOT MD")
    spec = load_omnigent_yaml(yaml_path)
    assert spec.instructions == "FROM AGENTS DOT MD"


# ── Terminals threading (OMNIGENT_TERMINAL_BRIDGE §6.1) ────────────


def test_terminals_thread_through_translator() -> None:
    """
    A top-level ``AgentDef.terminals`` dict is preserved under
    ``AgentSpec.terminals``. This is the load-bearing path that
    makes ``terminals:`` declarations in omnigent YAML reach the
    AP-side ``sys_terminal_*`` tools — the whole feature from
    ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` collapses if this breaks.

    What breaks if this fails: omnigent YAMLs that declare
    ``terminals:`` boot under Omnigent mode with
    ``AgentSpec.terminals=None``. The AP-side ToolManager doesn't
    register ``sys_terminal_*``, and the LLM gets a "tool not
    available" error mid-conversation.
    """
    from omnigent.inner.datamodel import (
        AgentDef,
        OSEnvSandboxSpec,
        OSEnvSpec,
        TerminalEnvSpec,
    )
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    bash_terminal = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd="/work",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        allow_cwd_override=True,
    )
    claude_terminal = TerminalEnvSpec(
        command="claude",
        args=["--dangerously-skip-permissions"],
        scrollback=20000,
    )
    parent = AgentDef(
        name="terminal_user",
        prompt="p",
        tools={},
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        terminals={
            "bash": bash_terminal,
            "claude": claude_terminal,
        },
    )

    spec = agent_def_to_agent_spec(parent)

    # The dict was copied (translator does ``dict(...)``), so
    # identity differs but contents match.
    assert spec.terminals is not None
    assert set(spec.terminals.keys()) == {"bash", "claude"}
    # The TerminalEnvSpec dataclasses themselves are passed by
    # reference — tools mutate via spec.terminals[name] and would
    # break if the translator deep-copied.
    assert spec.terminals["bash"] is bash_terminal
    assert spec.terminals["claude"] is claude_terminal


def test_terminals_none_when_parent_has_no_terminals() -> None:
    """
    A parent without a ``terminals`` block produces
    ``AgentSpec.terminals=None`` (not ``{}``). The
    :class:`SysTerminalLaunchTool` checks
    ``self._spec.terminals is None`` to short-circuit — an empty
    dict would fail the ``is None`` check but still render the
    same "no terminals declared" semantics with a confusingly
    different error message.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="no_terminals",
        prompt="p",
        tools={},
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        # No terminals.
    )
    spec = agent_def_to_agent_spec(parent)
    assert spec.terminals is None


def test_inline_agent_tool_inherits_parent_terminals() -> None:
    """
    Inline :class:`AgentTool` sub-specs inherit the parent's
    ``terminals`` declaration so the sub-agent's
    :class:`ToolManager` registers ``sys_terminal_*`` and the
    sub-agent can spawn its own sessions.

    Before: this test pinned the opposite behavior ("inline
    sub-agents must NOT inherit terminals"). That made the
    supervisor pattern broken end-to-end — a parent that wanted
    to delegate "open a shell and run X" to a worker had no way
    to grant the worker the launch capability, so workers
    either hallucinated tool calls or fell back to the
    harness's native ``Bash`` (which doesn't show up in the
    REPL's Ctrl+O sidebar). The user-reported repro on
    2026-04-28 hit exactly this.

    What changed: ``_agent_tool_to_sub_spec`` now threads
    ``parent_terminals=agent_def.terminals`` into the
    sub-spec's ``AgentSpec.terminals``. Each sub-agent runs
    in its OWN conversation, so its tmux sessions land in a
    separate ``TerminalRegistry`` — there's no cross-agent
    session leak (the registry is keyed by ``conversation_id``).
    Sharing the terminal CONFIG (``bash: command bash``) is
    fine; what's isolated is the per-conversation session set
    those launches produce.

    What breaks if this regresses: same as before — the
    sub-agent's ``ToolManager`` short-circuits the
    ``sys_terminal_*`` registration (see
    ``omnigent/tools/manager.py:426``) and the sub-agent has
    no way to launch a terminal even though the parent has one
    configured. The supervisor pattern stops working again.
    """
    from omnigent.inner.datamodel import AgentDef, TerminalEnvSpec
    from omnigent.inner.datamodel import (
        ExecutorSpec as OmniExecutorSpec,
    )
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="supervisor",
        prompt="p",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        terminals={"bash": TerminalEnvSpec(command="bash")},
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(harness="claude-sdk"),
            ),
        },
    )

    spec = agent_def_to_agent_spec(parent)
    assert spec.terminals is not None
    # Top-level got the terminals.
    assert "bash" in spec.terminals
    # The inline sub-agent inherits them.
    assert len(spec.sub_agents) == 1
    sub = spec.sub_agents[0]
    assert sub.terminals is not None, (
        "Inline AgentTool sub-agent must inherit the parent's "
        "terminals dict so its ToolManager registers "
        "sys_terminal_* and the sub-agent has a path to launch."
    )
    assert "bash" in sub.terminals
    # Verify it's a clone, not the same dict — mutations on
    # the sub-spec mustn't leak back into the parent's
    # declaration.
    assert sub.terminals is not spec.terminals


# ── Harness auto-pick (Gap 1) ─────────────────────────────────


@pytest.mark.parametrize(
    "model,expected_harness",
    [
        ("databricks-claude-sonnet-4", "claude-sdk"),
        ("databricks-claude-opus-4-7", "claude-sdk"),
        ("anthropic/claude-sonnet-4-20250514", "claude-sdk"),
        ("databricks-gpt-5-4", "openai-agents"),
        ("databricks-gpt-5-mini", "openai-agents"),
        ("openai/gpt-4o", "openai-agents"),
        ("gpt-4-turbo", "openai-agents"),
    ],
)
def test_harness_auto_picks_from_model_prefix(
    model: str,
    expected_harness: str,
) -> None:
    """
    When an omnigent YAML declares a model but no harness,
    the adapter fills in the right harness by matching the
    model prefix against
    :data:`~omnigent.spec.omnigent._HARNESS_FOR_MODEL_PREFIX`.

    Mirrors the auto-pick pure omnigent' CLI does at
    ``create_executor`` time, so YAMLs that relied on the
    implicit behavior don't need to be touched to work under
    Omnigent mode.

    What breaks if this fails: every YAML lacking an explicit
    ``harness:`` field trips the validator's
    ``executor.config.harness: required`` error at spec-load,
    blocking the entire Omnigent path.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = AgentDef(
        name="auto_pick_probe",
        prompt="",
        tools={},
        executor=OmniExecutorSpec(model=model),
    )
    spec = agent_def_to_agent_spec(agent_def)
    assert spec.executor.config["harness"] == expected_harness, (
        f"Adapter didn't auto-pick harness for model {model!r}. "
        f"Expected {expected_harness!r}, got "
        f"{spec.executor.config.get('harness')!r}"
    )


def test_harness_auto_pick_doesnt_override_explicit_declaration() -> None:
    """
    When the YAML explicitly declares a harness, auto-pick must
    NOT override it. Explicit always wins — same precedence
    rule as the profile and os_env fallbacks.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = AgentDef(
        name="explicit_probe",
        prompt="",
        tools={},
        executor=OmniExecutorSpec(
            model="databricks-claude-sonnet-4",
            harness="openai-agents",
        ),
    )
    spec = agent_def_to_agent_spec(agent_def)
    assert spec.executor.config["harness"] == "openai-agents", (
        "Explicit harness in the YAML was overridden by auto-pick "
        "— auto-pick must only fill in *missing* harness values."
    )


def test_harness_auto_pick_unknown_model_raises() -> None:
    """
    A model string that doesn't match any harness prefix raises
    at translation time — every agent must resolve to a named
    harness.

    :raises OmnigentError: With a message explaining that the
        model could not be mapped to a harness.
    """
    from omnigent.errors import OmnigentError
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = AgentDef(
        name="unknown_probe",
        prompt="",
        tools={},
        executor=OmniExecutorSpec(model="exotic/some-new-model-v1"),
    )
    with pytest.raises(OmnigentError, match=r"[Hh]arness"):
        agent_def_to_agent_spec(agent_def)


# ── Parent-to-inline harness propagation (Gap 2) ───────────


def test_inline_agent_tool_without_executor_inherits_parent_harness() -> None:
    """
    An inline :class:`AgentTool` that omits the ``executor:``
    block entirely inherits the parent's harness at translation
    time.

    Matches the YAML idiom in
    ``examples/coding_supervisor_with_forks.yaml`` and
    ``examples/agent_with_subagent_session.yaml``: workers
    declare ``prompt:`` + ``os_env:`` + ``tools:`` but skip
    ``executor:``, expecting the parent's harness to flow down.

    What breaks if this fails: those YAMLs fail at spec-load
    with ``sub_agents[...].executor.config.harness: required``
    before any LLM request.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
            profile="test-profile",
        ),
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="Do a task",
            ),
        },
    )
    spec = agent_def_to_agent_spec(parent)

    assert len(spec.sub_agents) == 1
    sub = spec.sub_agents[0]
    assert sub.executor.config["harness"] == "openai-agents", (
        "Sub-agent's harness should inherit from parent when the "
        "inline AgentTool omits ``executor:``."
    )


def test_inline_agent_tool_explicit_harness_wins_over_parent() -> None:
    """
    When the inline AgentTool declares its own harness, parent
    inheritance must NOT override it. Explicit always wins.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(
                    model="databricks-claude-opus-4-7",
                    harness="claude-sdk",
                ),
            ),
        },
    )
    spec = agent_def_to_agent_spec(parent)

    assert spec.sub_agents[0].executor.config["harness"] == "claude-sdk", (
        "Explicit child harness should win over parent inheritance."
    )


def test_inline_agent_tool_falls_through_to_model_auto_pick() -> None:
    """
    When neither the child NOR the parent declares a harness,
    the adapter's model-prefix auto-pick still fires.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec
    from omnigent.inner.tools import AgentTool
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    parent = AgentDef(
        name="supervisor",
        prompt="",
        executor=OmniExecutorSpec(model="databricks-gpt-5-mini"),
        tools={
            "worker": AgentTool(
                name="worker",
                prompt="",
                executor=OmniExecutorSpec(
                    model="databricks-claude-opus-4-7",
                ),
            ),
        },
    )
    spec = agent_def_to_agent_spec(parent)

    assert spec.executor.config["harness"] == "openai-agents"
    assert spec.sub_agents[0].executor.config["harness"] == "claude-sdk", (
        "Child with a Claude model and no harness (and no parent "
        "harness to inherit) should resolve through model auto-pick."
    )


# ── Policy translation (omnigent YAML → GuardrailsSpec) ────
#
# These tests exercise the raw-YAML-based translator
# (:func:`_translate_guardrails_yaml` and its dispatch helpers).
# Each test hand-constructs the minimal omnigent YAML dict
# that triggers one translation rule, routes it through
# :func:`agent_def_to_agent_spec` with ``raw_yaml=``, and
# asserts the resulting :class:`GuardrailsSpec` has the exact
# shape the runtime :class:`PolicyEngine` needs.
#
# The ``AgentDef`` side is built manually (not through
# :func:`omnigent.loader.load_agent_def`) because the loader
# compiles label-policy YAML into synthetic FunctionPolicy
# callables — for translator unit tests we want to bypass that
# and drive the translator directly.


class _AgentDefYamlPair(NamedTuple):
    """
    Two-value bundle for policy-translator tests — an
    :class:`AgentDef` and the raw YAML dict it was built from.

    Kept as a :class:`typing.NamedTuple` rather than a dataclass
    (per the project's "one opaque value" exception in the no-tuple-
    return rule) so existing callsites can keep ``agent_def,
    raw_yaml = _build_agent_def_with_raw_yaml(...)`` destructuring
    — the pair is conceptually a single "spec fixture" handed to
    ``agent_def_to_agent_spec(raw_yaml=...)``.
    """

    agent_def: AgentDef
    raw_yaml: dict[str, Any]


def _build_agent_def_with_raw_yaml(
    policies: dict[str, dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
    label_schema: dict[str, dict[str, Any]] | None = None,
    ask_timeout: int | None = None,
) -> _AgentDefYamlPair:
    """
    Build an :class:`AgentDef` + raw-YAML dict pair for the
    policy translator tests.

    The two objects are the shape production callers pass to
    :func:`agent_def_to_agent_spec`: an AgentDef parsed by the
    omnigent loader (here stubbed with an empty ``policies``
    registry so the fail-loud path is irrelevant — the real
    translator consumes ``raw_yaml`` for policy fields anyway)
    and the raw YAML dict read alongside.

    :param policies: Raw omnigent ``policies:`` dict.
    :param labels: Raw omnigent top-level ``labels:`` dict
        (initial values).
    :param label_schema: Raw omnigent top-level
        ``label_schema:`` dict.
    :param ask_timeout: Raw omnigent top-level
        ``ask_timeout:`` value.
    :returns: Tuple of (AgentDef with a valid executor, raw
        YAML dict suitable for the ``raw_yaml`` kwarg).
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec

    agent_def = AgentDef(
        name="polled",
        prompt="p",
        executor=OmniExecutorSpec(
            model="databricks-gpt-5-mini",
            harness="openai-agents",
        ),
    )
    raw: dict[str, Any] = {"name": "polled", "prompt": "p"}
    if policies is not None:
        raw["policies"] = policies
    if labels is not None:
        raw["labels"] = labels
    if label_schema is not None:
        raw["label_schema"] = label_schema
    if ask_timeout is not None:
        raw["ask_timeout"] = ask_timeout
    return _AgentDefYamlPair(agent_def=agent_def, raw_yaml=raw)


def test_function_policy_routes_callable_through_legacy_shim() -> None:
    """
    A ``type: function`` policy translates to a
    :class:`FunctionPolicySpec` whose ``function.path`` points
    at the legacy-compat shim factory; the author's original
    dotted callable path is preserved in
    ``function.arguments["target"]``.

    The indirection exists so author callables written with the
    legacy omnigent ``(content, phase)`` convention keep
    working under Omnigent' ``(ctx, context)`` convention —
    see ``omnigent.spec._omnigent_legacy_shim``. The shim
    is a runtime no-op for omnigent-native callables, so
    routing everything through it is safe.

    What breaks if this fails: either the translator regresses
    to emitting the raw callable path (legacy callables silently
    stop working — e.g. ``block_long_sleep`` lets every sleep
    through), or the shim target gets mangled and the
    ``importlib.import_module`` call inside ``build()`` can't
    find the author's callable.
    """
    from omnigent.spec.types import FunctionPolicySpec

    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "block_sleep": {
                "type": "function",
                "on": ["tool_call"],
                "handler": "tests.resources.examples._shared.tool_functions.block_long_sleep",
            },
        },
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.policies is not None
    assert len(spec.guardrails.policies) == 1
    policy = spec.guardrails.policies[0]
    assert isinstance(policy, FunctionPolicySpec)
    assert policy.name == "block_sleep"
    assert policy.function is not None
    # Factory path is the shim — exact string so a typo in the
    # translator can't route to some other builder silently.
    assert policy.function.path == "omnigent.spec._omnigent_legacy_shim.build"
    # The author's original callable travels in factory arguments under
    # the ``target`` key; no ``factory_kwargs`` because the YAML didn't
    # declare ``factory_params``.
    assert policy.function.arguments == {
        "target": "tests.resources.examples._shared.tool_functions.block_long_sleep",
    }
    # on: is ignored for function policies — callable self-selects.
    assert policy.on is None


def test_function_policy_with_factory_params_routes_through_legacy_shim() -> None:
    """
    ``callable:`` + ``factory_params:`` together still route
    through the shim, but the author's factory kwargs land
    under ``factory_kwargs`` in the shim's arguments so the
    shim can forward them when calling the author's factory.

    What breaks if this fails: factory kwargs silently vanish
    and closure-state policies (rate limits, budgets, etc.)
    revert to their defaults — the policy still loads but
    enforces nothing useful.
    """
    from omnigent.spec.types import FunctionPolicySpec

    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "rate_limit": {
                "type": "function",
                "on": ["tool_call"],
                "handler": (
                    "tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn"
                ),
                "factory_params": {"limit": 15},
            },
        },
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.policies is not None
    policy = spec.guardrails.policies[0]
    assert isinstance(policy, FunctionPolicySpec)
    assert policy.function is not None
    assert policy.function.path == "omnigent.spec._omnigent_legacy_shim.build"
    # Both the original target AND the factory kwargs are
    # preserved byte-for-byte on the arguments dict.
    assert policy.function.arguments == {
        "target": "tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn",
        "factory_kwargs": {"limit": 15},
    }


def test_function_policy_callable_alias_resolves_identically_to_handler() -> None:
    """
    ``callable:`` is a legacy alias for ``handler:`` in function policies.

    Old omnigent YAMLs used ``callable:`` as the key name; current
    YAMLs use ``handler:``. Both must produce the same
    :class:`FunctionPolicySpec` so stored agents written before the
    rename keep working without migration.

    What breaks if this fails: loading any old-format YAML raises
    ``"function policies require a function: field"`` and the agent
    refuses to start — the user has no path to run their agent without
    manually editing every stored YAML.
    """
    from omnigent.spec.types import FunctionPolicySpec

    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "block_sleep": {
                "type": "function",
                "on": ["tool_call"],
                "callable": "tests.resources.examples._shared.tool_functions.block_long_sleep",
            },
        },
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.policies is not None
    assert len(spec.guardrails.policies) == 1
    policy = spec.guardrails.policies[0]
    assert isinstance(policy, FunctionPolicySpec)
    assert policy.name == "block_sleep"
    assert policy.function is not None
    assert policy.function.path == "omnigent.spec._omnigent_legacy_shim.build"
    assert policy.function.arguments == {
        "target": "tests.resources.examples._shared.tool_functions.block_long_sleep",
    }


def test_function_policy_callable_alias_with_factory_params() -> None:
    """
    ``callable:`` + ``factory_params:`` together behave identically
    to ``handler:`` + ``factory_params:``.

    What breaks if this fails: old-format policies that include
    factory kwargs (e.g. ``read_all: true``) silently lose their
    configuration and revert to defaults.
    """
    from omnigent.spec.types import FunctionPolicySpec

    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "google_policy": {
                "type": "function",
                "on": ["tool_call", "tool_result"],
                "callable": (
                    "tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn"
                ),
                "factory_params": {"limit": 5},
            },
        },
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.policies is not None
    policy = spec.guardrails.policies[0]
    assert isinstance(policy, FunctionPolicySpec)
    assert policy.function is not None
    assert policy.function.path == "omnigent.spec._omnigent_legacy_shim.build"
    assert policy.function.arguments == {
        "target": "tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn",
        "factory_kwargs": {"limit": 5},
    }


def test_databricks_slash_model_without_profile_leaves_connection_none() -> None:
    """
    When no profile is declared, the translator leaves
    :attr:`LLMConfig.connection` as ``None`` and the
    :class:`DatabricksAdapter` performs its own auto-resolution from
    ``~/.databrickscfg`` at call time.

    **What breaks if this fails**: users who rely on ambient
    ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` or the default profile
    (no ``--profile`` flag) suddenly get a spec-load error because the
    translator tries to resolve a profile that was never set.
    """
    from omnigent.inner.datamodel import AgentDef
    from omnigent.inner.datamodel import ExecutorSpec as OmniExecutorSpec

    agent_def = AgentDef(
        name="slash_model_no_profile",
        prompt="You are helpful.",
        executor=OmniExecutorSpec(
            model="databricks/databricks-gpt-5-mini",
            harness="openai-agents",
            profile=None,
        ),
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=None)
    assert spec.llm is not None
    # No profile → no connection injection; adapter auto-resolves at call time.
    assert spec.llm.connection is None


def test_labels_and_schema_merge_and_monotonic_maps() -> None:
    """
    Top-level ``labels:`` (initial values) and ``label_schema:``
    (values + monotonic) merge into Omnigent'
    :attr:`GuardrailsSpec.labels` as :class:`LabelDef` entries,
    and ``monotonic: max`` / ``min`` map to ``increasing`` /
    ``decreasing``.

    What breaks if this fails: the workflow runs with the wrong
    schema — monotonic constraint inverted, initial value lost,
    or the label missing entirely.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        labels={"confidentiality": "0", "integrity": "1"},
        label_schema={
            "confidentiality": {"values": ["0", "1"], "monotonic": "max"},
            "integrity": {"values": ["0", "1"], "monotonic": "min"},
        },
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.labels is not None
    conf = spec.guardrails.labels["confidentiality"]
    integ = spec.guardrails.labels["integrity"]
    assert conf.initial == "0"
    assert conf.values == ["0", "1"]
    assert conf.monotonic == "increasing"
    assert integ.initial == "1"
    assert integ.values == ["0", "1"]
    # ``min`` → ``decreasing`` — the inverse of confidentiality.
    # If these got swapped or either mapped to None, the
    # workflow would accept label writes the YAML intended to
    # forbid, silently widening the trust boundary.
    assert integ.monotonic == "decreasing"


def test_ask_timeout_top_level_propagates_to_guardrails() -> None:
    """
    The omnigent top-level ``ask_timeout:`` lands on
    :attr:`GuardrailsSpec.ask_timeout`.

    What breaks if this fails: ASK-action policies park forever
    (no timeout) or fall back to the omnigent default even
    when the YAML author explicitly set one.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "noop": {
                "type": "function",
                "on": ["request"],
                "handler": "tests.resources.examples._shared.tool_functions.block_long_sleep",
            },
        },
        ask_timeout=60,
    )
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is not None
    assert spec.guardrails.ask_timeout == 60


def test_no_policies_no_guardrails_block() -> None:
    """
    An omnigent YAML without any policies/labels/ask_timeout
    produces a spec with ``guardrails=None``, matching the
    zero-policy case documented in POLICIES.md §10.

    What breaks if this fails: omnigent would instantiate a
    non-trivial :class:`PolicyEngine` for a guardrail-free spec,
    wasting work on every enforcement phase (and potentially
    masking real policy gaps downstream).
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.guardrails is None


def test_executor_extra_field_propagates_to_llm_config() -> None:
    """
    An omnigent YAML declaring ``executor.extra: {max_turns: 3}``
    produces an :class:`AgentSpec` whose ``llm.extra`` carries
    those kwargs byte-for-byte.

    The downstream chain (``OmnigentExecutor.run_turn`` →
    ``OmniExecutorConfig.extra`` → ``cfg.extra.get("max_turns")``
    in the per-harness executor) then reads these kwargs at
    runtime. This field is not part of the omnigent
    ``ExecutorSpec`` dataclass — the loader drops it — so we
    read it from the raw YAML here.

    What breaks if this fails: agent authors lose the ability to
    override per-harness knobs (``max_turns``, ``temperature``,
    ``parallel_tool_calls``, etc.) through the Omnigent path, even
    though those knobs work fine via legacy omnigent. Makes
    Omnigent mode a downgrade rather than a compatible integration.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "databricks-gpt-5-mini",
        "harness": "openai-agents",
        "extra": {"max_turns": 3, "temperature": 0.1},
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.llm is not None
    assert spec.llm.extra == {"max_turns": 3, "temperature": 0.1}


def test_executor_extra_absent_yields_empty_llm_extra() -> None:
    """
    When the omnigent YAML omits ``executor.extra``, the
    synthesized ``llm.extra`` is an empty dict (not ``None``,
    not missing). Matches the downstream code's assumption
    that ``dict(llm_config.extra)`` is always iterable.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.llm is not None
    assert spec.llm.extra == {}


def test_use_responses_false_propagates_to_executor_config() -> None:
    """
    ``use_responses: false`` in an omnigent YAML executor block lands on
    ``spec.executor.config["use_responses"]`` as ``False`` after
    ``agent_def_to_agent_spec``.

    The inner ``ExecutorSpec`` dataclass (``omnigent.inner.datamodel``)
    has no ``use_responses`` field, so the omnigent YAML loader silently
    drops it. We must read it from the raw YAML dict and carry it forward
    explicitly.

    What breaks if this fails: ``_build_openai_agents_sdk_spawn_env`` finds
    ``spec.executor.config.get("use_responses")`` is ``None``, so it skips
    setting ``HARNESS_OPENAI_AGENTS_USE_RESPONSES``. The harness subprocess
    then defaults to ``use_responses=True`` (Responses API), which Databricks
    does not support for models like Kimi K2 — the REPL shows no response.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "databricks-kimi-k2-6",
        "harness": "openai-agents",
        "use_responses": False,
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.executor.config.get("use_responses") is False


def test_use_responses_true_propagates_to_executor_config() -> None:
    """
    ``use_responses: true`` similarly propagates as ``True``.

    Complement of ``test_use_responses_false_propagates_to_executor_config``
    — verifies both boolean values are preserved exactly, not coerced.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "databricks-gpt-5-4-mini",
        "harness": "openai-agents",
        "use_responses": True,
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.executor.config.get("use_responses") is True


def test_use_responses_absent_omits_key_from_executor_config() -> None:
    """
    When the omnigent YAML omits ``use_responses``, the key is absent
    from ``spec.executor.config`` (not ``None``, not ``True``/``False``).

    ``_build_openai_agents_sdk_spawn_env`` uses ``config.get("use_responses")
    is not None`` to decide whether to set the env var; a missing key means
    the harness default applies unchanged.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "databricks-gpt-5-4-mini",
        "harness": "openai-agents",
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert "use_responses" not in spec.executor.config


def test_antigravity_vertex_config_propagates_to_executor_config() -> None:
    """
    ``executor.config`` vertex/project/location in an omnigent YAML land on
    ``spec.executor.config`` after ``agent_def_to_agent_spec``.

    Like ``use_responses``, none of these are fields on the inner
    ``ExecutorSpec`` dataclass (``omnigent.inner.datamodel``), so the omnigent
    YAML loader silently drops them. We must read them from the raw YAML dict
    and carry them forward explicitly.

    What breaks if this fails: ``_build_antigravity_spawn_env`` finds
    ``config.get("vertex")`` falsy, so it never sets ``HARNESS_ANTIGRAVITY_VERTEX``
    / ``_PROJECT`` / ``_LOCATION``. The documented Vertex config shape
    (``executor.config.vertex/project/location``) then silently does nothing for
    users who follow the docs — the harness falls back to ambient Gemini creds.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "gemini-2.0-flash",
        "harness": "antigravity",
        "vertex": True,
        "project": "my-gcp-project",
        "location": "us-central1",
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert spec.executor.config.get("vertex") is True
    assert spec.executor.config.get("project") == "my-gcp-project"
    assert spec.executor.config.get("location") == "us-central1"


def test_antigravity_vertex_config_reaches_spawn_env() -> None:
    """
    A carried-through Vertex ``executor.config`` produces the
    ``HARNESS_ANTIGRAVITY_VERTEX`` / ``_PROJECT`` / ``_LOCATION`` spawn env.

    End-to-end guard tying the adapter carry-through to the consumer: the keys
    must not only survive translation but also reach
    ``_build_antigravity_spawn_env``, which is what the docs promise.
    """
    from omnigent.runtime.workflow import _build_antigravity_spawn_env

    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "gemini-2.0-flash",
        "harness": "antigravity",
        "vertex": True,
        "project": "my-gcp-project",
        "location": "us-central1",
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    env = _build_antigravity_spawn_env(spec)
    assert env["HARNESS_ANTIGRAVITY_VERTEX"] == "1"
    assert env["HARNESS_ANTIGRAVITY_PROJECT"] == "my-gcp-project"
    assert env["HARNESS_ANTIGRAVITY_LOCATION"] == "us-central1"


def test_antigravity_vertex_config_absent_omits_keys_from_executor_config() -> None:
    """
    When the omnigent YAML omits vertex/project/location, none of the keys
    appear in ``spec.executor.config`` (a non-Vertex executor stays untouched).

    ``_build_antigravity_spawn_env`` keys off ``config.get("vertex")``; a missing
    key leaves the api-key / ambient path in force.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml()
    raw_yaml["executor"] = {
        "model": "gemini-2.0-flash",
        "harness": "antigravity",
    }
    spec = agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert "vertex" not in spec.executor.config
    assert "project" not in spec.executor.config
    assert "location" not in spec.executor.config


def test_unknown_policy_type_rejected_with_clear_message() -> None:
    """
    A policy with an unrecognized ``type:`` value fails with an
    error that names the policy and the invalid type.

    What breaks if this fails: the translator either silently
    drops the policy or produces an error deep in the
    downstream parser — authors can't tell which YAML key broke.
    """
    agent_def, raw_yaml = _build_agent_def_with_raw_yaml(
        policies={
            "weird": {
                "type": "regex",
                "on": ["request"],
            },
        },
    )
    with pytest.raises(OmnigentError, match="weird") as exc_info:
        agent_def_to_agent_spec(agent_def, raw_yaml=raw_yaml)
    assert "regex" in str(exc_info.value)


# ── Self-clone sub-agent (`tools.<name>: self` and `spec: self`) ──


def test_self_clone_string_shorthand_loader_produces_selfagent_tool(
    tmp_path: Path,
) -> None:
    """
    The ``tools.<name>: self`` string shorthand parses to a
    :class:`SelfAgentTool` (not the legacy
    :class:`AgentTool` placeholder with a magic prompt).

    What breaks if this fails: the translator's isinstance
    dispatch can't find a self-clone branch and the sub-agent
    silently becomes a default-everything AgentTool — the
    cloned-from-parent behavior is lost.
    """
    from omnigent.inner.loader import load_agent_def
    from omnigent.inner.tools import SelfAgentTool

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "code_assistant",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                },
                "tools": {
                    "subtask": "self",
                },
            }
        )
    )
    agent_def = load_agent_def(yaml_path)
    assert isinstance(agent_def.tools["subtask"], SelfAgentTool)
    assert agent_def.tools["subtask"].name == "subtask"


def test_self_clone_dict_form_loader_produces_selfagent_tool(
    tmp_path: Path,
) -> None:
    """
    The ``tools.<name>: {type: agent, spec: self}`` dict form
    parses to a :class:`SelfAgentTool`. Author-supplied
    ``description`` is preserved on the tool for the translator
    to thread into the cloned sub-spec.

    What breaks if this fails: the dict form would produce a
    regular :class:`AgentTool` with empty fields, and the
    translator would build a default sub-agent instead of
    cloning the parent.
    """
    from omnigent.inner.loader import load_agent_def
    from omnigent.inner.tools import SelfAgentTool

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "code_assistant",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                },
                "tools": {
                    "subtask": {
                        "type": "agent",
                        "spec": "self",
                        "description": "Spawn a copy of yourself for delegated work.",
                    },
                },
            }
        )
    )
    agent_def = load_agent_def(yaml_path)
    sub_tool = agent_def.tools["subtask"]
    assert isinstance(sub_tool, SelfAgentTool)
    assert sub_tool.name == "subtask"
    assert sub_tool.description == "Spawn a copy of yourself for delegated work."


def test_self_clone_dict_form_rejects_conflicting_overrides(
    tmp_path: Path,
) -> None:
    """
    ``spec: self`` cannot be combined with override fields
    (``prompt``, ``tools``, ``executor``, ``os_env``,
    ``pass_history``, ``pass_histories``, ``max_sessions``).

    What breaks if this fails: silently ignoring an override
    field would let an author write a partial-override-on-clone
    that doesn't actually do anything — confusing UX. The error
    message names the conflicting field so the author can fix
    the YAML.
    """
    from omnigent.inner.loader import load_agent_def

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "code_assistant",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                },
                "tools": {
                    "subtask": {
                        "type": "agent",
                        "spec": "self",
                        # Conflicting field — should fail loudly:
                        "prompt": "I want my own prompt.",
                    },
                },
            }
        )
    )
    with pytest.raises(ValueError, match="prompt") as exc:
        load_agent_def(yaml_path)
    # Message names the conflicting field AND points the author
    # at the right fix (use type: agent with explicit fields).
    msg = str(exc.value)
    assert "spec: self" in msg
    assert "subtask" in msg


def test_agent_def_to_agent_spec_self_clone_propagates_parent_config(
    tmp_path: Path,
) -> None:
    """
    Translating an omnigent YAML with ``tools.subtask: self``
    produces a sub-agent spec that's a clone of the parent —
    same model, harness, instructions, executor type. The
    parent's ``tools.agents`` lists the sub-agent's name so the
    LLM can dispatch to it via ``sys_session_send(agent="subtask",
    ...)``.

    What breaks if this fails: the LLM either can't see the
    sub-agent (it's missing from ``tools.agents``) OR the
    sub-agent runs with default-everything rather than
    inheriting the parent's harness/model/prompt — both render
    self-clone unusable in practice.
    """
    from omnigent.inner.loader import load_agent_def

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "code_assistant",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                },
                "tools": {
                    "subtask": {
                        "type": "agent",
                        "spec": "self",
                        "description": "Spawn a copy of yourself for delegated work.",
                    },
                },
            }
        )
    )
    agent_def = load_agent_def(yaml_path)
    spec = agent_def_to_agent_spec(agent_def)

    # Parent surface — sub-agent listed for sys_session_send dispatch.
    assert spec.tools.agents == ["subtask"]
    assert len(spec.sub_agents) == 1

    sub = spec.sub_agents[0]
    # Sub-agent's name matches the dispatch key.
    assert sub.name == "subtask"
    # Author's description override threads onto the sub-spec.
    assert sub.description == "Spawn a copy of yourself for delegated work."
    # Parent's prompt ports as the sub's instructions.
    assert sub.instructions == "You are a coding assistant."
    # Parent's model + harness propagate.
    assert sub.llm is not None
    assert sub.llm.model == "databricks-claude-sonnet-4-6"
    assert sub.executor.type == OMNIGENT_EXECUTOR_TYPE
    assert sub.executor.config["harness"] == "claude-sdk"


def test_agent_def_to_agent_spec_self_clone_recursion_guard(
    tmp_path: Path,
) -> None:
    """
    The cloned sub-spec does NOT carry its own self-clone tool —
    parser-time recursion is bounded to one level via
    :func:`_self_agent_tool_to_sub_spec`'s strip-then-recurse
    pattern.

    What breaks if this fails: the parse-time spec graph would
    grow without bound (clone has subtask which has subtask
    which has subtask...) producing an infinite recursion at
    ``agent_def_to_agent_spec`` and OOM-killing the process.

    Note: runtime recursion (a clone spawning another clone) is
    a separate concern and is intentionally bounded by per-
    workflow ``max_iterations``, not by this guard. This test
    pins the parse-time invariant only.
    """
    from omnigent.inner.loader import load_agent_def

    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "code_assistant",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                },
                "tools": {
                    "subtask": "self",
                },
            }
        )
    )
    agent_def = load_agent_def(yaml_path)
    spec = agent_def_to_agent_spec(agent_def)
    sub = spec.sub_agents[0]
    # Recursion guard: clone has no self-clone of its own.
    assert sub.tools.agents == []
    assert len(sub.sub_agents) == 0


def test_compat_yaml_executor_auth_is_not_dropped(tmp_path: Path) -> None:
    """
    ``executor.auth:`` declared in an omnigent-compat YAML is preserved
    on the resulting :class:`ExecutorSpec`, not silently dropped.

    Regression target: ``_translate_executor_from_def`` previously never
    called ``_parse_executor_auth``, so a YAML with
    ``executor.auth: {type: databricks, profile: oss}`` produced a spec
    with ``executor.auth = None``, causing it to fall through to the
    global config default (wrong credentials silently).

    Uses ``load_omnigent_yaml`` — the real production entry point — so
    the ``raw_yaml`` dict is passed through correctly (same as the real
    CLI path does).

    :param tmp_path: Temporary directory for the test YAML.
    """
    from omnigent.spec._omnigent_compat import load_omnigent_yaml
    from omnigent.spec.types import DatabricksAuth

    yaml_path = tmp_path / "agent_with_auth.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "databricks_agent",
                "prompt": "You are a coding assistant.",
                "executor": {
                    "model": "databricks-claude-sonnet-4-6",
                    "harness": "claude-sdk",
                    "auth": {"type": "databricks", "profile": "oss"},
                },
            }
        )
    )

    spec = load_omnigent_yaml(yaml_path)

    # auth must survive the compat translation — not silently dropped.
    assert isinstance(spec.executor.auth, DatabricksAuth), (
        "executor.auth was not parsed from the compat YAML; "
        "the agent would silently fall through to global config credentials."
    )
    assert spec.executor.auth.profile == "oss"


def test_compat_yaml_executor_api_key_auth_is_not_dropped(tmp_path: Path) -> None:
    """
    ``executor.auth: {type: api_key, …}`` in an omnigent-compat YAML is
    preserved on the resulting :class:`ExecutorSpec`.

    Uses ``load_omnigent_yaml`` — the real production entry point — so
    the ``raw_yaml`` dict is passed through correctly.

    :param tmp_path: Temporary directory for the test YAML.
    """
    from omnigent.spec._omnigent_compat import load_omnigent_yaml
    from omnigent.spec.types import ApiKeyAuth

    yaml_path = tmp_path / "agent_api_key.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "openai_agent",
                "prompt": "You are a test agent.",
                "executor": {
                    "harness": "openai-agents",
                    "auth": {"type": "api_key", "api_key": "sk-test-key"},
                },
            }
        )
    )

    spec = load_omnigent_yaml(yaml_path)

    assert isinstance(spec.executor.auth, ApiKeyAuth)
    assert spec.executor.auth.api_key == "sk-test-key"
