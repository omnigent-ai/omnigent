"""Policy hooks tests for Codex app server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import (
    _POLICY_HOOK_TIMEOUT_SECONDS,
    CodexAppServerClient,
    CodexNativeAppServer,
    _codex_policy_hooks_settings,
    _hooks_list_diagnostics,
    _our_policy_hooks_from_list,
    trust_native_policy_hooks,
)
from omnigent.harnesses.codex_native.hook import _EVALUATE_POLICY_TIMEOUT_S
from tests.harnesses.codex_native.app_server._support import (
    _CWD,
    _OUR_COMMAND,
    _USER_COMMAND,
    _batchwrite_calls,
    _disable_codex_startup_rpc,
    _fake_wait_until_ready,
    _FakeCodexClient,
    _hook,
    _set_codex_version,
    _test_app_server,
)


def test_hooks_list_empty_result_does_not_fall_back_to_envelope() -> None:
    """A valid empty result remains authoritative over envelope metadata."""
    listed = {
        "result": {},
        "data": [{"cwd": _CWD, "hooks": [_hook("k1", _OUR_COMMAND, "trusted")]}],
    }

    assert _our_policy_hooks_from_list(listed, _CWD) == []
    assert "returned no hooks" in _hooks_list_diagnostics(listed, _CWD)


async def test_untrusted_hook_is_trusted_via_batchwrite() -> None:
    """
    An untrusted Omnigent hook is trusted with its currentHash.

    This is the core flow: list → write trusted_hash → verify trusted.
    It fails if the batchWrite omits our key, writes the wrong hash, or
    skips the re-verification (which would let a still-untrusted hook
    through, silently disabling enforcement).
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "untrusted", "sha256:abc")])
    await trust_native_policy_hooks(client, cwd=_CWD)

    writes = _batchwrite_calls(client)
    assert len(writes) == 1  # exactly one trust write issued
    edit = writes[0].params["edits"][0]
    assert edit["keyPath"] == "hooks.state"
    assert edit["mergeStrategy"] == "upsert"
    # The written trusted_hash must equal the hook's reported currentHash.
    assert edit["value"] == {"k1": {"trusted_hash": "sha256:abc"}}
    # reloadUserConfig is required so the running thread hot-reloads trust.
    assert writes[0].params["reloadUserConfig"] is True


async def test_already_trusted_hook_skips_batchwrite() -> None:
    """
    A hook already trusted issues no config write.

    Avoids a redundant config.toml write + reload on every session start.
    Fails if the flow writes trust unconditionally.
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "trusted")])
    await trust_native_policy_hooks(client, cwd=_CWD)
    assert _batchwrite_calls(client) == []  # nothing to trust → no write


def test_write_codex_policy_hooks_file_merges_user_hooks(tmp_path: Path) -> None:
    """User hooks symlinked into the private home are merged into hooks.json.

    _write_codex_policy_hooks_file replaces the symlink with a merged
    regular file containing both the Omnigent policy hooks and the user's
    hooks, so user hooks fire alongside policy enforcement.
    """
    from omnigent.harnesses.codex_native.app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    # Simulate what _populate_codex_home_config does: symlink the user's hooks.json
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
    )
    (codex_home / "hooks.json").symlink_to(user_hooks)

    _write_codex_policy_hooks_file(codex_home, bridge_dir, sys.executable)

    hooks_path = codex_home / "hooks.json"
    assert not hooks_path.is_symlink(), "symlink must be replaced by a regular file"
    payload = json.loads(hooks_path.read_text())
    hooks = payload["hooks"]
    # Policy hooks present
    assert "PreToolUse" in hooks
    assert "PostToolUse" in hooks
    assert "UserPromptSubmit" in hooks
    # User's SessionStart hook merged in
    assert "SessionStart" in hooks
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "echo hi"


def test_write_codex_policy_hooks_file_no_symlink_unchanged(tmp_path: Path) -> None:
    """Without a symlink, hooks.json is written with only policy hooks."""
    from omnigent.harnesses.codex_native.app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    _write_codex_policy_hooks_file(codex_home, bridge_dir, sys.executable)

    payload = json.loads((codex_home / "hooks.json").read_text())
    assert set(payload["hooks"]) == {"PreToolUse", "PostToolUse", "UserPromptSubmit"}


def test_write_codex_policy_hooks_file_merges_router_hooks(tmp_path: Path) -> None:
    """Routing hooks share the one hooks.json codex loads, user hooks kept."""
    from omnigent.harnesses.codex_native.app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}'
    )

    _write_codex_policy_hooks_file(
        codex_home,
        bridge_dir,
        sys.executable,
        router_bridge_dir=router_dir,
        router_session_id="conv_abc",
        user_hooks_source=user_hooks,
    )

    hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for entry in hooks["PreToolUse"] for h in entry["hooks"]]
    assert any("codex_router_hook" in c and "--session-id conv_abc" in c for c in commands)
    assert any("codex_policy_hook" in c or "policy" in c for c in commands)
    # The user's own hook survives alongside our route-subagent gate.
    assert hooks["Stop"][0]["hooks"][0]["command"] == "echo bye"


def test_user_prompt_submit_carries_the_route_turn_hook(tmp_path: Path) -> None:
    """First-message routing rides the UserPromptSubmit entry codex trusts.

    Pins the launch-path invariant a UI-created terminal session depends on:
    the ``route-turn`` command is registered, points at the SAME bridge dir
    the runner advertises ``turn_router.json`` in, and lives under the
    policy-hook module so the trust handshake covers it. A hook codex loads
    but never trusts is a silent fail-open, and one pointed at a different
    directory finds no advertisement and falls open too.
    """
    from omnigent.harnesses.codex_native.app_server import (
        _POLICY_HOOK_MODULE,
        _our_policy_hooks_from_list,
        _write_codex_policy_hooks_file,
    )
    from omnigent.runner.turn_routing import HARNESS_HOOK_TIMEOUT_S

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"UserPromptSubmit": [{"hooks": '
        '[{"type": "command", "command": "echo mine"}]}]}}'
    )

    _write_codex_policy_hooks_file(
        codex_home,
        bridge_dir,
        sys.executable,
        user_hooks_source=user_hooks,
        turn_routing=True,
    )

    hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
    commands = [h for entry in hooks["UserPromptSubmit"] for h in entry["hooks"]]
    routing = [h for h in commands if "route-turn" in h["command"]]
    assert len(routing) == 1
    assert f"--bridge-dir {bridge_dir}" in routing[0]["command"]
    assert "--harness codex-native" in routing[0]["command"]
    assert routing[0]["timeout"] == HARNESS_HOOK_TIMEOUT_S
    # Trust is filtered by module, so route-turn must ride the policy one.
    assert _POLICY_HOOK_MODULE in routing[0]["command"]
    listed = {"result": {"data": [{"cwd": "/repo", "hooks": commands}]}}
    assert len(_our_policy_hooks_from_list(listed, "/repo")) == 2
    # The user's own prompt hook survives the merge.
    assert any(h["command"] == "echo mine" for h in commands)


async def test_missing_hook_raises() -> None:
    """
    No discovered Omnigent hook fails loud (anti fail-open).

    If our hook was never registered/loaded, enforcement would silently
    not run. The flow must raise rather than return quietly. Fails if a
    missing hook is tolerated.
    """
    # Only a user-owned hook is present; ours is absent.
    client = _FakeCodexClient(hooks=[_hook("u1", _USER_COMMAND, "untrusted")])
    with pytest.raises(RuntimeError, match="not discovered"):
        await trust_native_policy_hooks(client, cwd=_CWD)
    # We must never touch a hook that isn't ours.
    assert _batchwrite_calls(client) == []


async def test_still_untrusted_after_write_raises() -> None:
    """
    A hook that stays untrusted after the write fails loud.

    Simulates a trust write that didn't take (e.g. hash mismatch). The
    flow must detect the still-untrusted state on re-list and raise, not
    proceed with a silently-skipped policy gate.
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "untrusted")], flip_on_trust=False)
    with pytest.raises(RuntimeError, match="still untrusted"):
        await trust_native_policy_hooks(client, cwd=_CWD)
    # The write was attempted before the failure was detected.
    assert len(_batchwrite_calls(client)) == 1


async def test_user_hooks_are_never_trusted() -> None:
    """
    Only Omnigent hooks are trusted; user-declared hooks are left alone.

    The private CODEX_HOME symlinks the user's config.toml, which may
    declare its own hooks. Auto-trusting those would be a security hole.
    Fails if the user's hook key appears in the trust write.
    """
    client = _FakeCodexClient(
        hooks=[
            _hook("ours", _OUR_COMMAND, "untrusted", "sha256:ours"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )
    await trust_native_policy_hooks(client, cwd=_CWD)
    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    written = writes[0].params["edits"][0]["value"]
    # Only our key is trusted; the user's hook is never touched.
    assert written == {"ours": {"trusted_hash": "sha256:ours"}}


# --- Enriched, self-diagnosing trust/discovery errors -----------------


async def test_missing_hook_error_reports_zero_hooks_loaded() -> None:
    """
    Discovery failure with no hooks loaded names the likely cause.

    When codex loads zero hooks (the symptom of an invalid per-session
    config.toml → codex falls back to defaults), the "not discovered"
    error must say so, not just report the bare cwd. Fails if the
    diagnostic suffix is dropped, which is what made the original report
    impossible to triage.
    """
    client = _FakeCodexClient(hooks=[])
    with pytest.raises(RuntimeError, match="loaded none"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_missing_hook_error_reports_module_mismatch() -> None:
    """
    Discovery failure with only foreign hooks reports "0 ours".

    If codex listed hooks for the cwd but none are ours (e.g. a stale /
    renamed hook command from an out-of-date install), the error must
    distinguish that from "no hooks at all". Fails if the per-entry
    ownership count is not surfaced.
    """
    client = _FakeCodexClient(hooks=[_hook("u1", _USER_COMMAND, "untrusted")])
    with pytest.raises(RuntimeError, match="0 ours"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_still_untrusted_error_includes_status_message() -> None:
    """
    A hook that stays untrusted surfaces codex's own statusMessage.

    Codex reports *why* a hook cannot be trusted in ``statusMessage``
    (e.g. a managed-hooks requirement rejecting a user hook). The trust
    handshake otherwise discards it; the error must carry it through so
    the cause is visible. Fails if statusMessage is not included.
    """
    hook = {
        "key": "k1",
        "command": _OUR_COMMAND,
        "trustStatus": "untrusted",
        "currentHash": "sha256:abc",
        "isManaged": False,
        "statusMessage": "managed hooks only",
    }
    client = _FakeCodexClient(hooks=[hook], flip_on_trust=False)
    with pytest.raises(RuntimeError, match="managed hooks only"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_still_untrusted_hints_old_codex_when_hash_missing() -> None:
    """
    Missing currentHash/trustStatus points at an old codex version.

    codex < 0.129 omits ``currentHash``/``trustStatus`` from
    ``hooks/list``; the trust write then writes nothing and the hook
    stays untrusted. The error must name the version cause rather than
    the misleading bare "still untrusted". Fails if the version hint is
    absent when the protocol fields are missing.
    """
    hook = {"key": "k1", "command": _OUR_COMMAND, "trustStatus": None, "currentHash": None}
    client = _FakeCodexClient(hooks=[hook])
    with pytest.raises(RuntimeError, match=r"older than 0\.129\.0"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_old_codex_skips_policy_hook_and_records_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    codex < 0.129 starts without registering the policy hook.

    The session must NOT be blocked (fail-open): start() returns, no
    hooks.json is written (codex could never trust it), and the reason is
    recorded for the web-UI notice. Fails if startup raises (the old
    blocking behavior) or if the hook is registered against an
    un-trustable codex.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 128, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    # ap_server_url present → enforcement was intended → this is the
    # security-relevant degrade path.
    server.ap_server_url = "http://127.0.0.1:9999"
    await server.start()
    try:
        # Hook was NOT registered: codex < 0.129 can never trust it.
        assert not (codex_home / "hooks.json").exists()
        # Reason is recorded so the caller can surface a web-UI notice.
        assert server.policy_hook_disabled_reason is not None
        assert "0.128.0" in server.policy_hook_disabled_reason
        assert "0.129.0" in server.policy_hook_disabled_reason
    finally:
        await server.close()


async def test_supported_codex_registers_hook_and_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    codex >= 0.129 registers the hook and reports enforcement active.

    Fails if the version gate wrongly disables a supported codex (which
    would silently drop enforcement on every modern session).
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    _set_codex_version(monkeypatch, (0, 129, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    try:
        # Hook registered for a supported codex.
        assert (codex_home / "hooks.json").exists()
        # None == enforcement active (no degrade reason).
        assert server.policy_hook_disabled_reason is None
    finally:
        await server.close()


async def test_unknown_codex_version_treated_as_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An unparseable codex version does not disable enforcement.

    A flaky/odd ``codex --version`` must not silently drop policy
    enforcement — we proceed to register + trust (a real trust failure is
    then caught separately). Fails if ``None`` is treated as "too old".
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    _set_codex_version(monkeypatch, None)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    try:
        assert (codex_home / "hooks.json").exists()
        assert server.policy_hook_disabled_reason is None
    finally:
        await server.close()


async def test_old_codex_with_routing_armed_keeps_user_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Arming subagent routing on old codex must not delete the user's hooks.

    On codex < 0.129 no generated hooks file is written, so the user's
    ``hooks.json`` has to stay symlinked into the private home. Fails if
    the routing arm drops the symlink and nothing takes its place.
    """
    from omnigent.inner.codex_executor import CODEX_ROUTER_DIR_ENV_VAR

    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    user_hooks = real_codex_home / "hooks.json"
    user_hooks.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}]}})
    )
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 128, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    server.env = {CODEX_ROUTER_DIR_ENV_VAR: str(router_dir)}
    await server.start()
    try:
        hooks_path = codex_home / "hooks.json"
        assert hooks_path.exists()
        assert json.loads(hooks_path.read_text())["hooks"]["Stop"]
        assert server.router_hooks_registered is False
    finally:
        await server.close()


async def test_trust_failure_is_fail_open_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A trust handshake failure degrades the session instead of blocking it.

    This is the core behavior change: a hook that can't be trusted (e.g.
    "not discovered" on an otherwise-supported codex) must NOT raise out
    of start() — the session runs, the reason is recorded for a web-UI
    notice. Fails if start() re-raises (the old blocking behavior) or
    leaves the reason unset.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 136, 0))

    async def _raise_trust(
        _self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
    ) -> None:
        del client
        raise RuntimeError("Omnigent policy hook was not discovered for cwd ...")

    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _raise_trust)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    server.ap_server_url = "http://127.0.0.1:9999"
    await server.start()  # must NOT raise
    try:
        # Hook was registered (supported codex) but trust failed → degrade.
        assert (codex_home / "hooks.json").exists()
        assert server.policy_hook_disabled_reason is not None
        # The underlying trust error is carried into the reason.
        assert "not discovered" in server.policy_hook_disabled_reason
        assert "could not be trusted" in server.policy_hook_disabled_reason
    finally:
        await server.close()


def test_policy_hooks_timeout_outlasts_the_hooks_request_budget() -> None:
    """The codex hook timeout must outlast the hook's own AP request budget.

    A TOOL_CALL ASK is resolved server-side: the hook's POST to
    ``/policies/evaluate`` blocks (up to ``_EVALUATE_POLICY_TIMEOUT_S``) while
    the server parks the gate as a URL elicitation. Codex kills the hook
    subprocess after the ``timeout`` it reads from ``hooks.json``. If that
    timeout were shorter than the request budget, codex would kill the hook
    mid-park and run the tool before the ASK verdict arrived — the regression
    that let sub-agent tool calls slip past the cost gate (it was 30s).
    """
    settings = _codex_policy_hooks_settings(Path("/b"), "/venv/bin/python")
    hooks = settings["hooks"]
    # All registered phases share the same command hook; assert the timeout on
    # each so none can silently regress independently. UserPromptSubmit gates
    # the request phase and also blocks on a server-side ASK park, so it needs
    # the same generous timeout as the tool phases.
    pre = hooks["PreToolUse"][0]["hooks"][0]
    post = hooks["PostToolUse"][0]["hooks"][0]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert pre["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    assert post["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    assert prompt["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    # The invariant that actually prevents the bug: codex must wait at least as
    # long as the hook itself will block on the server. If this fails (e.g. the
    # constant is dropped back to 30), the gate becomes advisory for every
    # native tool call, sub-agent or not.
    assert _POLICY_HOOK_TIMEOUT_SECONDS >= _EVALUATE_POLICY_TIMEOUT_S


def test_policy_hooks_register_user_prompt_submit() -> None:
    """The request-phase gate must be wired onto UserPromptSubmit.

    For native sessions the server-level ``_evaluate_input_policy`` skips
    message events, so this hook is the sole REQUEST gate. If it were dropped
    from ``hooks.json``, native prompts (web-UI-injected and direct-terminal
    alike) would reach the model with no request-phase policy at all.
    """
    settings = _codex_policy_hooks_settings(Path("/b"), "/venv/bin/python")
    hooks = settings["hooks"]
    assert "UserPromptSubmit" in hooks
    prompt_hook = hooks["UserPromptSubmit"][0]["hooks"][0]
    # Same evaluate-policy command as the tool phases.
    assert prompt_hook["command"] == hooks["PreToolUse"][0]["hooks"][0]["command"]
