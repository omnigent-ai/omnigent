"""Unit tests for HermesAcpExecutor (Hermes layer over the generic AcpExecutor).

The generic ACP client (transport, streaming, permission plumbing) is covered
by ``tests/inner/test_acp_executor.py``; these tests cover only what the Hermes
subclass adds:

- Construction: ``hermes acp --accept-hooks`` command, streaming capabilities
- Per-session HERMES_HOME wiring (policy hook) exposed via ``_spawn_env``
- Permission requests auto-allowed (the pre_tool_call hook is the gate)
- ``session/new`` extras (model / restrictive skills filter) with reject-retry
- Usage normalization: cache-inclusive ``inputTokens`` split
- Self-executed vs MCP-bridge tool-call classification and metadata stamping
- Registry wiring: hermes-acp routes to the ACP module with ACP capabilities
"""

from __future__ import annotations

import os

import pytest

from omnigent.inner.hermes_acp_executor import HermesAcpExecutor


def test_defaults_and_capabilities() -> None:
    ex = HermesAcpExecutor(hermes_path="hermes")
    assert ex._config.command == "hermes acp --accept-hooks"
    spaced = HermesAcpExecutor(hermes_path="/opt/my tools/hermes")
    assert spaced._argv[0] == "/opt/my tools/hermes"  # survives shlex.split
    assert ex._config.name == "Hermes Agent"
    assert ex.supports_streaming() is True
    assert ex.handles_tools_internally() is True


@pytest.mark.asyncio
async def test_close_with_no_process_is_noop() -> None:
    await HermesAcpExecutor(hermes_path="hermes").close()  # must not raise


def test_setup_hermes_home_populates_hook_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a server URL and conversation id available, the executor creates a
    per-session HERMES_HOME with the policy hook wired (same as the batch path),
    and _spawn_env passes it to the subprocess."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://localhost:8000")
    monkeypatch.setattr(
        "sys.argv", ["harness", "--conversation-id", "conv_test123"], raising=False
    )
    ex = HermesAcpExecutor(hermes_path="hermes")
    try:
        assert ex._hermes_home is not None
        assert (ex._hermes_home / "config.yaml").is_file()
        assert (ex._hermes_home / "omnigent-policy-hook.sh").is_file()
        assert ex._spawn_env().get("HERMES_HOME") == str(ex._hermes_home)
    finally:
        import shutil

        if ex._hermes_home is not None:
            shutil.rmtree(ex._hermes_home, ignore_errors=True)


def test_setup_hermes_home_disabled_without_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    ex = HermesAcpExecutor(hermes_path="hermes")
    assert ex._hermes_home is None
    # No per-session home -> the spawn env carries no override of its own.
    assert ex._spawn_env().get("HERMES_HOME") == os.environ.get("HERMES_HOME")


@pytest.mark.asyncio
async def test_permission_auto_allowed_only_when_hook_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the hook wired, permission requests are allowed beneath it (never
    policy-evaluated twice). WITHOUT the hook, the generic policy/elicitation
    route applies -- auto-allow would run ungated."""
    from pathlib import Path

    req = {"toolCall": {"title": "terminal: rm -rf /", "rawInput": {"command": "rm -rf /"}}}

    hooked = HermesAcpExecutor(hermes_path="hermes")
    hooked._hermes_home = Path("/tmp/fake-home")
    assert await hooked._decide_permission(req) is True

    unhooked = HermesAcpExecutor(hermes_path="hermes")
    assert unhooked._hermes_home is None
    seen: list[str] = []

    async def generic(self, params):
        seen.append("generic")
        return False

    monkeypatch.setattr("omnigent.inner.acp_executor.AcpExecutor._decide_permission", generic)
    assert await unhooked._decide_permission(req) is False
    assert seen == ["generic"]


@pytest.mark.asyncio
async def test_rejected_skills_filter_fails_closed() -> None:
    """If the agent rejects a restrictive skills filter, session/new raises --
    launching with every skill enabled would silently widen policy."""
    ex = HermesAcpExecutor(hermes_path="hermes", skills_filter=["review"])

    async def fake_rpc(method: str, params: dict, timeout: float) -> dict:
        if "skills" in params:
            return {"error": {"message": "unknown field: skills"}}
        return {"result": {"sessionId": "s-1"}}

    ex._rpc = fake_rpc  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="skills"):
        await ex._ensure_session()


@pytest.mark.asyncio
async def test_model_retry_keeps_skills_filter() -> None:
    """A rejected model retries WITHOUT the model but WITH the skills filter."""
    ex = HermesAcpExecutor(hermes_path="hermes", model="m-1", skills_filter=["review"])
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float) -> dict:
        calls.append(dict(params))
        if "model" in params:
            return {"error": {"message": "unknown field: model"}}
        return {"result": {"sessionId": "s-1"}}

    ex._rpc = fake_rpc  # type: ignore[method-assign]
    sid = await ex._ensure_session()
    assert sid == "s-1"
    assert calls[0].get("model") == "m-1" and calls[0].get("skills") == ["review"]
    assert "model" not in calls[1] and calls[1].get("skills") == ["review"]


@pytest.mark.asyncio
async def test_session_new_applies_model_then_falls_back() -> None:
    """A model override is sent in session/new; on rejection it retries without."""
    ex = HermesAcpExecutor(hermes_path="hermes", model="anthropic/claude-sonnet-4")
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float) -> dict:
        calls.append({"method": method, "params": params})
        if method == "session/new" and "model" in params:
            return {"error": {"message": "unknown field: model"}}
        return {"result": {"sessionId": "s-1"}}

    ex._rpc = fake_rpc  # type: ignore[method-assign]
    sid = await ex._ensure_session()
    assert sid == "s-1"
    news = [c for c in calls if c["method"] == "session/new"]
    assert news[0]["params"].get("model") == "anthropic/claude-sonnet-4"  # tried with model
    assert "model" not in news[1]["params"]  # retried without, not silently dropped


@pytest.mark.asyncio
async def test_skills_filter_sent_and_all_not_sent() -> None:
    """A restrictive skills_filter goes in session/new params; 'all' is omitted."""
    seen: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float) -> dict:
        seen.append(params)
        return {"result": {"sessionId": "s-1"}}

    ex = HermesAcpExecutor(hermes_path="hermes", skills_filter=["review"])
    ex._rpc = fake_rpc  # type: ignore[method-assign]
    await ex._ensure_session()
    assert seen[0].get("skills") == ["review"]

    seen.clear()
    ex2 = HermesAcpExecutor(hermes_path="hermes", skills_filter="all")
    ex2._rpc = fake_rpc  # type: ignore[method-assign]
    await ex2._ensure_session()
    assert "skills" not in seen[0]


@pytest.mark.asyncio
async def test_session_setup_classifies_native_and_bridged_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = HermesAcpExecutor(hermes_path="hermes")
    ex._omnigent_tools = [{"name": "sys_session_get_info"}]
    monkeypatch.setattr(ex._mcp, "session_new_servers", lambda **_: [{"name": "omnigent"}])

    async def fake_rpc(method: str, params: dict, timeout: float) -> dict:
        return {"result": {"sessionId": "s-1"}}

    ex._rpc = fake_rpc  # type: ignore[method-assign]
    await ex._ensure_session()

    native = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "native-1",
            "title": "terminal: date",
            "rawInput": {"command": "date"},
        }
    )[0]
    bridged = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "bridge-1",
            "title": "Session info",
            "rawInput": {"tool": "mcp_omnigent_sys_session_get_info"},
        }
    )[0]

    assert native.metadata == {"call_id": "native-1", "self_executed": True}
    assert bridged.metadata == {"call_id": "bridge-1"}


def test_usage_normalized_with_cache_split() -> None:
    """inputTokens is cache-inclusive; the cached portion splits out so the
    cost accumulator prices it correctly."""
    u = HermesAcpExecutor._usage_from_result(
        {
            "usage": {
                "inputTokens": 29068,
                "outputTokens": 50,
                "totalTokens": 29118,
                "cachedReadTokens": 1000,
            }
        }
    )
    assert u == {
        "input_tokens": 28068,  # 29068 - 1000 cached
        "cache_read_input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 29118,
    }
    assert HermesAcpExecutor._usage_from_result({"usage": "nope"}) is None


def test_registry_and_capabilities() -> None:
    from omnigent.harness_capabilities import IntegrationMode
    from omnigent.harness_plugins import _BUILTIN_CONTRIBUTION, harness_modules

    assert "hermes-acp" in _BUILTIN_CONTRIBUTION.valid_harnesses
    assert harness_modules()["hermes-acp"] == "omnigent.inner.hermes_acp_harness"
    caps = _BUILTIN_CONTRIBUTION.capabilities["hermes-acp"]
    assert caps.integration_mode is IntegrationMode.ACP_SUBPROCESS
    assert caps.streaming is True
    assert caps.interrupt is True
