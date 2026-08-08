"""Tests for the host launch-gate harness resolution.

A spec embedding a one-shot ``executor.acp_agent`` is self-contained — the
command rides in the spec — so both launch paths must send ``harness=None``
on the ``host.launch_runner`` frame, the documented fail-open that skips the
host's configured-harness refusal. Without it, a repo-declared ACP agent
could never launch on a host with no global ``acp:`` config.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.server.routes._sessions.helpers import _harness_for_launch_gate
from omnigent.server.routes.hosts import _resolve_agent_harness
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec

_EMBEDDED_EXECUTOR = {
    "type": "omnigent",
    "acp_agent": {"name": "Repo Echo", "command": "echo-agent --acp"},
    "config": {"harness": "acp:repo-echo"},
}


def _parse_spec(tmp_path: Path, executor: dict[str, object]) -> AgentSpec:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"spec_version": 1, "name": "agent", "executor": executor})
    )
    return parse(tmp_path)


def _stubs(
    spec: AgentSpec,
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    """Duck-typed conv/agent_store/agent_cache carrying the given spec."""
    agent = SimpleNamespace(id="agent1", bundle_location="/bundles/agent1", session_id=None)
    agent_store = SimpleNamespace(get=lambda agent_id: agent)
    agent_cache = SimpleNamespace(
        load=lambda agent_id, location, expand_env=True: SimpleNamespace(spec=spec)
    )
    conv = SimpleNamespace(
        id="conv1",
        agent_id="agent1",
        harness_override=None,
        sub_agent_name=None,
    )
    return conv, agent_store, agent_cache


# ── create-launch path (POST /v1/hosts/{id}/runners) ─────────────────────


@pytest.mark.asyncio
async def test_embedded_acp_agent_skips_launch_gate(tmp_path: Path) -> None:
    spec = _parse_spec(tmp_path, _EMBEDDED_EXECUTOR)
    conv, store, cache = _stubs(spec)
    assert await _resolve_agent_harness(conv, store, cache) is None


@pytest.mark.asyncio
async def test_plain_acp_slug_still_gated(tmp_path: Path) -> None:
    """Without an embed, the slug resolves via global config — keep the gate."""
    spec = _parse_spec(tmp_path, {"type": "omnigent", "config": {"harness": "acp:gemini-cli"}})
    conv, store, cache = _stubs(spec)
    assert await _resolve_agent_harness(conv, store, cache) == "acp"


@pytest.mark.asyncio
async def test_non_acp_harness_unaffected_by_embed_check(tmp_path: Path) -> None:
    spec = _parse_spec(tmp_path, {"type": "omnigent", "config": {"harness": "claude-sdk"}})
    conv, store, cache = _stubs(spec)
    assert await _resolve_agent_harness(conv, store, cache) == "claude-sdk"


# ── relaunch path (_harness_for_launch_gate) ─────────────────────────────


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    store: SimpleNamespace,
    cache: SimpleNamespace,
) -> None:
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", store)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)


def test_relaunch_gate_skips_embedded_acp_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = _parse_spec(tmp_path, _EMBEDDED_EXECUTOR)
    conv, store, cache = _stubs(spec)
    _patch_runtime(monkeypatch, store, cache)
    assert _harness_for_launch_gate(conv) is None


def test_relaunch_gate_keeps_plain_acp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = _parse_spec(tmp_path, {"type": "omnigent", "config": {"harness": "acp:gemini-cli"}})
    conv, store, cache = _stubs(spec)
    _patch_runtime(monkeypatch, store, cache)
    assert _harness_for_launch_gate(conv) == "acp"


def test_relaunch_gate_keeps_non_acp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = _parse_spec(tmp_path, {"type": "omnigent", "config": {"harness": "claude-sdk"}})
    conv, store, cache = _stubs(spec)
    _patch_runtime(monkeypatch, store, cache)
    assert _harness_for_launch_gate(conv) == "claude-sdk"
