"""Structural test for the PipesHub example (examples/pipeshub).

PipesHub is a single-agent recipe that answers questions over an
organization's PipesHub-connected knowledge sources (Slack, Drive,
Confluence, etc.) through PipesHub's MCP server, wired via a directory
``tools/mcp/pipeshub.yaml`` config. Pure spec-load — no LLM, no MCP
server, no live PipesHub deployment. ``PIPESHUB_MCP_URL`` /
``PIPESHUB_MCP_TOKEN`` are set to dummy values per test so the
``${VAR}`` references resolve the way they do for a real user.

What breaks if this fails:
- the recipe silently pins a model (re-coupling it to one provider),
- the harness drifts off ``claude-sdk`` (the recipe is deliberately pinned
  to it — see the config.yaml comment on why),
- a sub-agent appears (this is deliberately a single agent, not an
  orchestrator, matching ``deep-research``'s "one agent + one MCP server"
  pattern rather than the Polly/Debby-class multi-agent examples),
- the ``pipeshub`` MCP server is dropped/renamed or stops being an HTTP
  (not stdio) connector,
- the ``url`` or ``Authorization`` header stops resolving from the
  environment, or a missing variable stops failing loudly,
- the bundle ``omnigent run`` uploads stops carrying the resolved ``url``
  and token (the server parses uploads with expansion off),
- an endpoint or secret gets hardcoded into the committed files,
- the example stops shipping as package data, so a ``pip`` / ``uv tool``
  install can no longer copy it out,
- ``AGENTS.md`` stops being picked up as instructions (a stray top-level
  ``prompt:`` in config.yaml would be silently ignored per the parser's
  ``instructions`` > ``prompt`` precedence — this asserts the file
  actually won that precedence, not just that it exists on disk).
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest
import yaml

from omnigent.cli import _bundle
from omnigent.errors import OmnigentError
from omnigent.server.bundles import validate_agent_bundle
from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_pipeshub.py -> repo root is 3 parents up.
_PIPESHUB_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "pipeshub"
_MCP_YAML = _PIPESHUB_BUNDLE / "tools" / "mcp" / "pipeshub.yaml"

_URL = "https://acme.pipeshub.test/mcp"
_TOKEN = "phpat_dummy_token_for_tests"


@pytest.fixture
def pipeshub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set both variables the example reads, as a user would before running it."""
    monkeypatch.setenv("PIPESHUB_MCP_URL", _URL)
    monkeypatch.setenv("PIPESHUB_MCP_TOKEN", _TOKEN)


@pytest.fixture
def pipeshub_spec(pipeshub_env: None) -> AgentSpec:
    """Load the pipeshub bundle with ``${VAR}`` expansion on, as a real run does."""
    return load(_PIPESHUB_BUNDLE, expand_env=True)


def test_pipeshub_name_and_harness(pipeshub_spec: AgentSpec) -> None:
    """
    The agent is named ``pipeshub`` and is deliberately pinned to the
    claude-sdk harness with no pinned model or profile.
    """
    assert pipeshub_spec.name == "pipeshub"
    assert pipeshub_spec.executor.config.get("harness") == "claude-sdk"
    assert pipeshub_spec.executor.model is None
    assert pipeshub_spec.executor.profile is None


def test_pipeshub_is_single_agent(pipeshub_spec: AgentSpec) -> None:
    """PipesHub is a single agent — no sub-agents, no delegation."""
    assert pipeshub_spec.sub_agents == []
    assert pipeshub_spec.tools.agents == []


def test_pipeshub_resolves_url_and_token_from_env(pipeshub_spec: AgentSpec) -> None:
    """
    The PipesHub MCP server is an HTTP connector whose ``url`` and
    ``Authorization`` header both resolve from the environment, so the
    agent reaches the user's deployment with the user's token.
    """
    assert [s.name for s in pipeshub_spec.mcp_servers] == ["pipeshub"]
    server = pipeshub_spec.mcp_servers[0]
    assert server.transport == "http"
    assert server.url == _URL
    assert server.headers.get("Authorization") == f"Bearer {_TOKEN}"


def test_pipeshub_upload_bundle_resolves_url_and_token(pipeshub_env: None) -> None:
    """
    ``omnigent run`` bundles the folder and the server parses that bundle
    with expansion off, so the upload itself must carry the resolved
    ``url`` and token.
    """
    spec = validate_agent_bundle(_bundle(_PIPESHUB_BUNDLE), enforce_handler_allowlist=False)
    server = next(s for s in spec.mcp_servers if s.name == "pipeshub")
    assert server.url == _URL
    assert server.headers.get("Authorization") == f"Bearer {_TOKEN}"


def test_pipeshub_committed_files_hold_only_templates() -> None:
    """
    The files on disk carry ``${VAR}`` templates, never an endpoint or a
    token, so the directory is safe to commit and share.
    """
    raw = yaml.safe_load(_MCP_YAML.read_text(encoding="utf-8"))
    assert raw["url"] == "${PIPESHUB_MCP_URL}"
    assert raw["headers"] == {"Authorization": "Bearer ${PIPESHUB_MCP_TOKEN}"}
    # Parsed values only: comments may show a placeholder endpoint as an example.
    for path in (_PIPESHUB_BUNDLE / "config.yaml", _MCP_YAML):
        values = yaml.safe_dump(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert "phpat_" not in values, path
        assert "://" not in values, path


@pytest.mark.parametrize("missing", ["PIPESHUB_MCP_URL", "PIPESHUB_MCP_TOKEN"])
def test_pipeshub_missing_variable_fails_loudly(
    pipeshub_env: None, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """
    An unset variable stops the load with an error naming it, instead of
    sending a literal ``${...}`` to PipesHub as the URL or token.
    """
    monkeypatch.delenv(missing)
    with pytest.raises(OmnigentError, match=rf"\$\{{{missing}\}}"):
        load(_PIPESHUB_BUNDLE, expand_env=True)


def test_pipeshub_ships_as_package_data() -> None:
    """
    The example is reachable through ``omnigent.resources.examples``, which
    is how the README's copy-out command finds it in an installed package.
    """
    packaged = importlib.resources.files("omnigent.resources.examples") / "pipeshub"
    for rel in ("config.yaml", "AGENTS.md", "README.md", "tools/mcp/pipeshub.yaml"):
        assert packaged.joinpath(rel).is_file(), rel


def test_pipeshub_instructions_come_from_agents_md(pipeshub_spec: AgentSpec) -> None:
    """
    ``AGENTS.md`` is auto-discovered as instructions (config.yaml has no
    ``prompt:`` key, so there is nothing for ``instructions:`` to lose
    precedence to — this asserts the file's content actually landed,
    not just that a file with that name exists on disk).
    """
    assert pipeshub_spec.instructions is not None
    assert "PipesHub research agent" in pipeshub_spec.instructions
    assert "Search before answering" in pipeshub_spec.instructions
