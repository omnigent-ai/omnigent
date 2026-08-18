from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_model_vocabulary import claude_model_alias
from omnigent.inner.hook_scripts import claude_router_hook, subagent_router
from tests.inner.conftest import advertise_relay_tools, advertise_router


def _payload(
    *,
    tool_name: str = "Agent",
    subagent_type: str = "code-reviewer",
    prompt: str = "review the diff",
    model: str | None = None,
) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"subagent_type": subagent_type, "prompt": prompt}
    if model is not None:
        tool_input["model"] = model
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }


def _run_hook_main(
    monkeypatch: pytest.MonkeyPatch,
    stdin: str,
    argv: list[str],
) -> str:
    """Drive ``claude_router_hook.main`` over *stdin* and return its stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert claude_router_hook.main(argv) == 0
    return out.getvalue()


def _no_router(monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    """Fail the test if the hook reaches the router at all."""

    def unreachable(*args: object, **kwargs: object) -> dict[str, Any] | None:
        raise AssertionError(why)

    monkeypatch.setattr(subagent_router, "request_decision", unreachable)


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    router_dir: Path,
    payload: dict[str, Any],
    decision: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Run the hook with a canned router *decision*; return output + requests."""
    seen: list[dict[str, Any]] = []

    def fake_request(
        endpoint: subagent_router.RouterEndpoint,
        session_id: str,
        body: dict[str, Any],
        *,
        timeout: float = 0.0,
    ) -> dict[str, Any] | None:
        seen.append({"endpoint": endpoint, "session_id": session_id, "body": body})
        return decision

    monkeypatch.setattr(subagent_router, "request_decision", fake_request)
    raw = _run_hook_main(monkeypatch, json.dumps(payload), ["--bridge-dir", str(router_dir)])
    return (json.loads(raw) if raw else None), seen


def test_rewrite_allows_with_routed_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    out, _requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(),
        {
            "action": "rewrite",
            "model": "databricks-claude-haiku-4-5",
            "raw_model": "router-vocab-model",
            "rationale": "cheapest arm",
            "decision_id": "dec-1",
        },
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "subagent_type": "code-reviewer",
                "prompt": "review the diff",
                # Claude's Agent tool takes tier aliases, never catalog ids.
                "model": "haiku",
            },
            "permissionDecisionReason": "cheapest arm (applied as 'haiku')",
        }
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-sonnet-5", "sonnet"),
        ("databricks-claude-sonnet-4-6", "sonnet"),
        ("databricks-claude-haiku-4-5", "haiku"),
        ("databricks-claude-opus-4-8", "opus"),
        ("databricks-claude-fable-5", "fable"),
        ("system.ai.claude-sonnet-5", "sonnet"),
        ("claude-opus-4-8[1m]", "opus"),
        ("sonnet", "sonnet"),
        ("databricks-gpt-5-5", None),
        ("mystery-model", None),
        ("", None),
    ],
)
def test_agent_tool_model_translation(model: str, expected: str | None) -> None:
    assert claude_model_alias(model, {}) == expected


def test_agent_tool_model_prefers_workspace_alias_pinning() -> None:
    # The workspace pins "sonnet" to a model whose own name says otherwise;
    # the env mapping is authoritative over the name heuristic.
    env = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-mystery-9"}
    assert claude_model_alias("databricks-claude-mystery-9", env) == "sonnet"


def test_untranslatable_model_allows_spawn_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id with no Agent-tool alias must not be injected — the CLI 400s."""
    router_dir = advertise_router(tmp_path)
    for env_var in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(env_var, raising=False)
    out, _requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(),
        {"action": "rewrite", "model": "mystery-model", "rationale": "r", "decision_id": "d"},
    )
    assert out is None


def test_bridge_recorded_pinning_gates_the_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch pinning recorded on the bridge decides what's spellable."""
    router_dir = advertise_router(tmp_path)
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "active_session_id": "conv_abc",
                # Only opus is pinned to a gateway id, so a routed sonnet has
                # no accepted spelling — "sonnet" would resolve to a vendor id
                # the gateway rejects.
                "model_env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"},
            }
        )
    )
    decision = {
        "action": "rewrite",
        "model": "databricks-claude-sonnet-5",
        "rationale": "r",
        "decision_id": "d",
    }
    out, _requests = _run_hook(monkeypatch, router_dir, _payload(), decision)
    assert out is None

    decision["model"] = "databricks-claude-opus-4-8"
    out, _requests = _run_hook(monkeypatch, router_dir, _payload(), decision)
    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"


def test_codex_style_output_keeps_the_catalog_id() -> None:
    """Without a translator the servable id is injected verbatim (codex)."""
    decision = {"action": "rewrite", "model": "databricks-gpt-5-5", "rationale": "r"}
    output = subagent_router.decision_to_hook_output(decision, {"task_name": "t"})
    assert output is not None
    assert output["hookSpecificOutput"]["updatedInput"] == {
        "task_name": "t",
        "model": "databricks-gpt-5-5",
    }


def _redirect_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Run a cross-harness redirect through the hook and return its deny reason."""
    out, _requests = _run_hook(
        monkeypatch,
        tmp_path,
        _payload(),
        {
            "action": "redirect",
            "model": "other-model",
            "harness": "codex",
            "rationale": "cross-harness pick",
            "decision_id": "dec-2",
        },
    )
    assert out is not None
    hook_output = out["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    reason = hook_output["permissionDecisionReason"]
    assert isinstance(reason, str)
    return reason


def test_redirect_denies_with_mcp_prefixed_session_create_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_router(tmp_path)
    advertise_relay_tools(tmp_path, "sys_session_create", "sys_agent_list", "sys_read_inbox")

    reason = _redirect_reason(tmp_path, monkeypatch)

    # The instruction must name the tool the way Claude advertises it. Claude
    # exposes Omnigent's MCP tools as ``mcp__omnigent__<tool>``, so the bare
    # name it used to quote made the model report the tool as nonexistent and
    # abandon the sub-task (live: session e26d94b2).
    assert "mcp__omnigent__sys_session_create" in reason
    assert "mcp__omnigent__sys_agent_list" in reason
    # No bare occurrence outside the prefixed spelling.
    assert "sys_session_create" not in reason.replace("mcp__omnigent__sys_session_create", "")
    assert "sys_agent_list" not in reason.replace("mcp__omnigent__sys_agent_list", "")
    assert "sys_session_send" not in reason
    assert "other-model" in reason
    assert "codex" in reason
    # Claude Code defers MCP schemas behind tool search, so the tool is absent
    # from the up-front list; the reason must say to search rather than assume.
    assert "omnigent" in reason
    assert "search" in reason.lower()


def test_redirect_without_the_spawn_tool_names_no_sys_session_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose relay has no spawn tool must not be told to call one.

    Naming an unheld tool is the failure this whole change fixes; the fallback
    has to name nothing and hand the work back to the current model.
    """
    advertise_router(tmp_path)
    advertise_relay_tools(tmp_path, "sys_read_inbox")

    reason = _redirect_reason(tmp_path, monkeypatch)

    assert "sys_session_" not in reason
    assert "sys_agent_list" not in reason
    assert "yourself" in reason
    assert "other-model" in reason


def test_redirect_without_a_relay_file_keeps_the_actionable_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``tool_relay.json`` means availability is unknown, not unavailable.

    Every harness without a relay (the claude-agent-sdk arm) must keep today's
    instruction rather than degrade to the do-it-yourself fallback.
    """
    advertise_router(tmp_path)
    assert not (tmp_path / subagent_router._TOOL_RELAY_FILE).exists()

    reason = _redirect_reason(tmp_path, monkeypatch)

    assert "mcp__omnigent__sys_session_create" in reason
    assert "yourself" not in reason


def test_deny_carries_router_rationale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    out, _requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(),
        {"action": "deny", "model": None, "rationale": "router unreachable", "decision_id": "d"},
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "router unreachable",
        }
    }


def test_allow_emits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    out, _requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(),
        {"action": "allow", "model": None, "rationale": "", "decision_id": "d"},
    )
    assert out is None


def test_fork_typed_spawn_routes_like_any_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router_dir = advertise_router(tmp_path)
    _out, requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(subagent_type="fork"),
        {"action": "allow", "rationale": "", "decision_id": "d"},
    )
    body = requests[0]["body"]
    assert body == {
        "harness": "claude-native",
        "task_name": "fork",
        "prompt": "review the diff",
        "parent_model": None,
        "requested_model": None,
    }


@pytest.mark.parametrize(
    ("asked", "forwarded"),
    [
        # Claude's Agent tool spells the ask as a family alias, so forwarding it
        # verbatim would compare "opus" against a catalog id and report every
        # honored ask as overridden.
        ("opus", "databricks-claude-opus-4-8"),
        ("OPUS", "databricks-claude-opus-4-8"),
        # Already a catalog id, which compares as-is: passed through.
        ("databricks-claude-opus-4-8", "databricks-claude-opus-4-8"),
        ("system.ai.claude-sonnet-5", "system.ai.claude-sonnet-5"),
        # An alias this session pins nothing to resolves to a vendor id we
        # cannot name, so the body claims no ask at all.
        ("sonnet", None),
        # Sentinels are not model asks.
        ("inherit", None),
        ("default", None),
    ],
)
def test_an_alias_ask_is_resolved_to_the_pinned_catalog_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asked: str,
    forwarded: str | None,
) -> None:
    router_dir = advertise_router(tmp_path)
    (tmp_path / "bridge.json").write_text(
        json.dumps(
            {
                "active_session_id": "conv_abc",
                "model_env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"},
            }
        )
    )
    _out, requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(model=asked),
        {"action": "allow", "rationale": "", "decision_id": "d"},
    )
    assert requests[0]["body"]["requested_model"] == forwarded


def test_endpoint_down_allows_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    out, _requests = _run_hook(monkeypatch, router_dir, _payload(), None)
    assert out is None


def test_missing_advertisement_allows_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(subagent_router.ROUTER_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(subagent_router.BRIDGE_DIR_ENV_VAR, raising=False)
    _no_router(monkeypatch, "router must not be called without an advertisement")
    stdout = _run_hook_main(monkeypatch, json.dumps(_payload()), ["--bridge-dir", str(tmp_path)])
    assert stdout == ""


@pytest.mark.parametrize(
    "url",
    [
        # Off-box exfiltration: the bridge dir is agent-writable, so an
        # advertisement naming a remote host would leak the spawn prompt.
        "http://evil.example.com:8080",
        # A non-http scheme is not our loopback runner either.
        "file:///tmp/x",
        "https://127.0.0.1:9000",
        # Not loopback, even though it is an IP literal.
        "http://10.0.0.5:9000",
    ],
)
def test_non_loopback_advertisement_is_rejected(tmp_path: Path, url: str) -> None:
    advertise_router(tmp_path, url=url)
    assert subagent_router.read_router_endpoint(tmp_path) is None


def test_advertisement_from_a_dead_pid_is_rejected(tmp_path: Path) -> None:
    """A stale advertisement's port can be re-bound by another process."""
    dead_pid = 2**22 - 1
    advertise_router(tmp_path, pid=dead_pid)
    assert subagent_router.read_router_endpoint(tmp_path) is None


def test_advertisement_from_a_live_pid_is_accepted(tmp_path: Path) -> None:
    advertise_router(tmp_path, pid=os.getpid())
    assert subagent_router.read_router_endpoint(tmp_path) is not None


@pytest.mark.parametrize("pid", [None, "1234", 0, -1, 1.5, True])
def test_advertisement_without_a_usable_pid_is_rejected(
    tmp_path: Path, pid: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runner always writes an int pid, so anything else is not ours."""
    advertise_router(tmp_path, pid=pid)
    assert subagent_router.read_router_endpoint(tmp_path) is None
    assert "pid not alive" in capsys.readouterr().err


def test_rejected_advertisements_explain_themselves_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    advertise_router(tmp_path, url="http://10.0.0.5:9000")
    assert subagent_router.read_router_endpoint(tmp_path) is None
    err = capsys.readouterr().err
    assert "not plain http on loopback" in err
    assert subagent_router.ADVERTISEMENT_FILE in err


@pytest.mark.parametrize(
    ("url", "pid"),
    [
        ("http://10.0.0.5:9000", None),
        ("http://127.0.0.1:9000/", 2**22 - 1),
    ],
)
def test_rejection_diagnostics_never_echo_the_advertisement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str, pid: object
) -> None:
    """The advertisement holds a bearer token, so nothing off it is logged."""
    advertise_router(tmp_path, url=url, pid=pid, token="s3cr3t-bearer-value")
    assert subagent_router.read_router_endpoint(tmp_path) is None
    err = capsys.readouterr().err
    assert err.strip()
    assert "s3cr3t-bearer-value" not in err
    assert url not in err


def test_other_tools_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    _no_router(monkeypatch, "non-spawn tools must not reach the router")
    stdout = _run_hook_main(
        monkeypatch,
        json.dumps(_payload(tool_name="Bash")),
        ["--bridge-dir", str(router_dir)],
    )
    assert stdout == ""


def test_legacy_task_tool_name_is_routed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router_dir = advertise_router(tmp_path)
    out, _requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(tool_name="Task"),
        {
            "action": "rewrite",
            "model": "databricks-claude-sonnet-5",
            "rationale": "",
            "decision_id": "d",
        },
    )
    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"


def test_malformed_stdin_allows_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_hook_main(monkeypatch, "not json", []) == ""


def test_session_id_falls_back_to_bridge_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router_dir = advertise_router(tmp_path, session_id=None)
    (tmp_path / "bridge.json").write_text(
        json.dumps({"active_session_id": "conv_from_bridge", "launch_model": "parent-model"})
    )
    monkeypatch.delenv(subagent_router.SESSION_ID_ENV_VAR, raising=False)
    monkeypatch.delenv(subagent_router.NATIVE_SESSION_ID_ENV_VAR, raising=False)
    _out, requests = _run_hook(
        monkeypatch,
        router_dir,
        _payload(),
        {"action": "allow", "rationale": "", "decision_id": "d"},
    )
    request = requests[0]
    assert request["session_id"] == "conv_from_bridge"
    assert request["body"]["parent_model"] == "parent-model"


def test_malformed_advertisement_is_treated_as_absent(tmp_path: Path) -> None:
    (tmp_path / subagent_router.ADVERTISEMENT_FILE).write_text("{not json")
    assert subagent_router.read_router_endpoint(tmp_path) is None
    (tmp_path / subagent_router.ADVERTISEMENT_FILE).write_text(json.dumps({"url": "u"}))
    assert subagent_router.read_router_endpoint(tmp_path) is None


def test_redirect_without_target_fails_open() -> None:
    decision = {"action": "redirect", "model": None, "harness": None, "rationale": "x"}
    assert subagent_router.decision_to_hook_output(decision, {}) is None


class _FakeHookMatcher:
    def __init__(self, *, matcher: str | None = None, hooks: list[Any], timeout: float) -> None:
        self.matcher = matcher
        self.hooks = hooks
        self.timeout = timeout


class _FakeSDK:
    HookMatcher = _FakeHookMatcher


class _FakeOptions:
    hooks: dict[str, list[Any]] | None = None


def _install() -> _FakeOptions:
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    options = _FakeOptions()
    ClaudeSDKExecutor()._install_subagent_router_hook(_FakeSDK(), options, "parent-model")  # type: ignore[arg-type]
    return options


def test_sdk_hook_registered_when_router_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_router(tmp_path)
    monkeypatch.setenv(subagent_router.ROUTER_DIR_ENV_VAR, str(tmp_path))
    options = _install()
    assert options.hooks is not None
    matcher = options.hooks["PreToolUse"][0]
    assert matcher.matcher == subagent_router.AGENT_TOOL_MATCHER
    # Strictly outside the router call's own budget: registered AT it, the SDK
    # could cancel the hook at the same instant its request gave up, so the
    # fail-open branch never ran and the harness saw a dead hook.
    assert matcher.timeout == subagent_router.HOOK_TIMEOUT_S
    assert matcher.timeout > subagent_router.REQUEST_TIMEOUT_S


def test_sdk_hook_not_registered_without_advertisement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(subagent_router.ROUTER_DIR_ENV_VAR, str(tmp_path))
    options = _install()
    assert options.hooks is None


async def test_sdk_callback_maps_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    advertise_router(tmp_path)
    monkeypatch.setenv(subagent_router.ROUTER_DIR_ENV_VAR, str(tmp_path))
    bodies: list[dict[str, Any]] = []

    def fake_request(
        endpoint: subagent_router.RouterEndpoint,
        session_id: str,
        body: dict[str, Any],
        *,
        timeout: float = 0.0,
    ) -> dict[str, Any]:
        bodies.append(body)
        return {"action": "rewrite", "model": "databricks-claude-sonnet-5", "rationale": "r"}

    monkeypatch.setattr(subagent_router, "request_decision", fake_request)
    options = _install()
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]
    output = await callback(_payload(), "toolu_1", {"signal": None})
    # The SDK callback shares the hook's translation: alias, not catalog id.
    assert output["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"
    assert bodies[0]["harness"] == "claude-sdk"
    assert bodies[0]["parent_model"] == "parent-model"


async def test_sdk_callback_allows_unchanged_when_router_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_router(tmp_path)
    monkeypatch.setenv(subagent_router.ROUTER_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(
        subagent_router,
        "request_decision",
        lambda *args, **kwargs: None,
    )
    options = _install()
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]
    assert await callback(_payload(), None, {"signal": None}) == {}


# ── Fail open, and fail fast ────────────────────────────────────────────────


def test_the_spawn_gate_asks_on_the_ladders_own_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The hook waits ``REQUEST_TIMEOUT_S`` for a verdict, and no longer.

    A spawn gate holds the parent agent's tool call open until it answers, so
    the budget IS the stall a wedged router costs. Asserted against the
    constant, not elapsed time, so the ladder is what is pinned.
    """
    router_dir = advertise_router(tmp_path)
    seen: list[float] = []

    def _timed_out(
        endpoint: subagent_router.RouterEndpoint,
        session_id: str,
        body: dict[str, Any],
        *,
        timeout: float = 0.0,
    ) -> dict[str, Any] | None:
        del endpoint, session_id, body
        seen.append(timeout)
        return None

    monkeypatch.setattr(subagent_router, "request_decision", _timed_out)
    raw = _run_hook_main(monkeypatch, json.dumps(_payload()), ["--bridge-dir", str(router_dir)])

    # "No opinion": the spawn runs on exactly the model it asked for.
    assert raw == ""
    assert seen == [subagent_router.REQUEST_TIMEOUT_S]
    # The budget must outlast a healthy route (catalog prep + router call) and
    # stay at or under the owner's 15s ceiling.
    assert subagent_router.REQUEST_TIMEOUT_S <= 15.0
    # The harness's own kill has to sit above it, or the fail-open branch above
    # never runs and the harness sees a dead hook instead of "no opinion".
    assert subagent_router.HOOK_TIMEOUT_S > subagent_router.REQUEST_TIMEOUT_S
    assert subagent_router.HOOK_TIMEOUT_S < 30


def test_an_unreachable_relay_is_no_opinion_not_a_dropped_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Nothing is listening on the advertised port, so the real transport runs.

    Exercises ``request_decision``'s own fail-open rather than a stubbed one:
    an urllib failure of any kind has to read as "no opinion", because the
    alternative is a spawn the agent asked for that never happens.
    """
    router_dir = advertise_router(tmp_path)
    endpoint = subagent_router.read_router_endpoint(router_dir)
    assert endpoint is not None
    assert (
        subagent_router.request_decision(endpoint, "conv_abc", {"harness": "claude-sdk"}) is None
    )

    raw = _run_hook_main(monkeypatch, json.dumps(_payload()), ["--bridge-dir", str(router_dir)])
    assert raw == ""
