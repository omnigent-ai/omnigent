"""Tests for codex-native app-server policy-hook trust handling."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from omnigent.codex_native_app_server import (
    _POLICY_HOOK_TIMEOUT_SECONDS,
    CodexNativeAppServer,
    _codex_policy_hooks_settings,
    build_codex_native_server,
    trust_native_policy_hooks,
)
from omnigent.codex_native_hook import _EVALUATE_POLICY_TIMEOUT_S

_CWD = "/home/user/repo"
_OUR_COMMAND = "/venv/bin/python -m omnigent.codex_native_hook evaluate-policy --bridge-dir /b"
_USER_COMMAND = "bash /home/user/.config/llm-cli/hooks/guard.sh"


def _hook(key: str, command: str, trust: str, current_hash: str = "sha256:h") -> dict[str, Any]:
    """
    Build a ``hooks/list`` hook metadata entry.

    :param key: Hook key, e.g. ``"/b/codex-home/hooks.json:pre_tool_use:0:0"``.
    :param command: Hook command string (used to identify ownership).
    :param trust: Trust status, e.g. ``"untrusted"`` / ``"trusted"``.
    :param current_hash: The hook's content hash, e.g. ``"sha256:h"``.
    :returns: A hook metadata dict shaped like ``hooks/list`` output.
    """
    return {
        "key": key,
        "command": command,
        "trustStatus": trust,
        "currentHash": current_hash,
    }


@dataclass
class _Req:
    """
    One recorded JSON-RPC request issued to the fake client.

    :param method: RPC method name, e.g. ``"hooks/list"``.
    :param params: RPC params dict.
    """

    method: str
    params: dict[str, Any]


@dataclass
class _FakeCodexClient:
    """
    Fake Codex app-server client scripted for the trust flow.

    Returns the current hook set for ``hooks/list`` and, on
    ``config/batchWrite``, flips a hook to ``trusted`` when the written
    ``trusted_hash`` matches the hook's ``currentHash`` (mirroring codex's
    real trust evaluation). ``flip_on_trust=False`` simulates a hash
    mismatch where trust never takes.

    :param hooks: Initial hook metadata (mutated as trust is written).
    :param flip_on_trust: Whether a matching trusted_hash flips trust.
    """

    hooks: list[dict[str, Any]]
    flip_on_trust: bool = True
    requests: list[_Req] = field(default_factory=list)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle one scripted RPC request.

        :param method: RPC method, e.g. ``"hooks/list"`` or
            ``"config/batchWrite"``.
        :param params: RPC params.
        :returns: A response envelope matching the real app-server shape.
        """
        self.requests.append(_Req(method=method, params=params))
        if method == "hooks/list":
            return {"result": {"data": [{"cwd": _CWD, "hooks": self.hooks}]}}
        if method == "config/batchWrite":
            if self.flip_on_trust:
                written = params["edits"][0]["value"]
                for hook in self.hooks:
                    update = written.get(hook["key"])
                    if update and update.get("trusted_hash") == hook["currentHash"]:
                        hook["trustStatus"] = "trusted"
            return {"result": {"status": "ok"}}
        raise AssertionError(f"unexpected RPC method {method!r}")


def _batchwrite_calls(client: _FakeCodexClient) -> list[_Req]:
    """
    Return the config/batchWrite requests the trust flow issued.

    :param client: The fake client after the flow ran.
    :returns: Recorded batchWrite requests (empty if none issued).
    """
    return [r for r in client.requests if r.method == "config/batchWrite"]


async def _fake_wait_until_ready(self: CodexNativeAppServer) -> None:
    """
    Skip app-server socket probing in startup unit tests.

    :param self: The app-server wrapper under test.
    :returns: None.
    """


async def _fake_trust_policy_hooks(self: CodexNativeAppServer) -> None:
    """
    Skip Codex ``hooks/list`` RPCs in startup unit tests.

    :param self: The app-server wrapper under test.
    :returns: None.
    """


def _disable_codex_startup_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch Codex startup RPC waits for unit tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _fake_trust_policy_hooks)


def test_build_codex_native_server_profile_error_names_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Missing Databricks profile errors identify the runner-visible profile.

    The native Codex terminal can fail before the TUI launches if the
    runner process cannot resolve the Databricks profile it was given.
    The message must include that profile name so operators can tell a
    stale/missing runner env apart from a generic Codex startup failure.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._read_databrickscfg",
        lambda _profile: None,
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "missing-databrickscfg"))

    with pytest.raises(OSError, match="profile 'oss'"):
        build_codex_native_server(
            socket_path=tmp_path / "codex.sock",
            codex_home=tmp_path / "codex-home",
            cwd=tmp_path,
            model=None,
            profile="oss",
            bridge_dir=tmp_path / "bridge",
            ap_server_url=None,
            ap_auth_headers={},
        )


def test_build_codex_native_server_uses_profile_host_without_static_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native Codex accepts Databricks CLI OAuth profiles without static tokens.

    A default Omnigent install may not include ``databricks-sdk`` in the
    runner process. In that case ``_read_databrickscfg`` cannot mint a bearer
    at startup, but the profile's host is still enough: Codex gets an
    ``auth.command`` that runs ``databricks auth token --profile`` at request
    time.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._read_databrickscfg",
        lambda _profile: None,
    )
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        "\n".join(
            [
                "[oss]",
                "host = https://example.cloud.databricks.com",
                "auth_type = databricks-cli",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    overrides = "\n".join(app_server.config_overrides)
    assert "https://example.cloud.databricks.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"oss\\"' in overrides


def _test_app_server(
    tmp_path: Path,
    codex_home: Path,
    bridge_dir: Path,
    workspace: Path,
) -> CodexNativeAppServer:
    """
    Build a Codex app-server wrapper for startup unit tests.

    :param tmp_path: Test temp directory, e.g. ``Path("/tmp/test")``.
    :param codex_home: Private Codex home to write.
    :param bridge_dir: Bridge directory for the generated MCP args.
    :param workspace: Working directory for the subprocess.
    :returns: Configured app-server wrapper.
    """
    return CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=codex_home,
        env={},
        config_overrides=[],
        cwd=workspace,
        bridge_dir=bridge_dir,
        python_executable="/new/python",
    )


async def test_start_upserts_mcp_server_config_across_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup upserts MCP config across relaunches.

    Repeated native terminal startup uses the same private ``CODEX_HOME``.
    The generated config must remain valid TOML and the user's real
    symlinked config must stay untouched.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    source_config = real_codex_home / "config.toml"
    original = """\
[projects."/repo"]
trust_level = "trusted"

[mcp_servers.omnigent] # stale generated table
command = "/old/python"
args = ["old"]

[mcp_servers.omnigent.env] # stale generated env
OLD = "1"

[mcp_servers.other]
command = "other"
args = []
"""
    source_config.write_text(original, encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()
    await server.start()
    await server.close()

    assert source_config.read_text(encoding="utf-8") == original
    config_path = codex_home / "config.toml"
    assert not config_path.is_symlink()
    rendered = config_path.read_text(encoding="utf-8")
    assert rendered.count("[mcp_servers.omnigent]") == 1
    assert "[mcp_servers.omnigent.env]" not in rendered
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
    }


async def test_start_writes_fresh_mcp_config_without_leading_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup writes fresh MCP config without leading blanks.

    Codex should be able to read a newly-created private ``config.toml``
    without cosmetic leading whitespace from the generated section
    separator logic.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()

    rendered = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert rendered.startswith("[mcp_servers.omnigent]\n")
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
    }


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


# --- Codex version gate + fail-open startup ---------------------------


def _set_codex_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] | None
) -> None:
    """
    Stub the codex version probe used by :meth:`start`.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param version: Version tuple to report, e.g. ``(0, 128, 0)``, or
        ``None`` to simulate an unparseable ``codex --version``.
    :returns: None.
    """

    async def _fake_version(_codex_path: str) -> tuple[int, int, int] | None:
        return version

    monkeypatch.setattr("omnigent.codex_native_app_server._codex_cli_version", _fake_version)


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

    async def _raise_trust(_self: CodexNativeAppServer) -> None:
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


class TestPinCodexConfigModel:
    """_pin_codex_config_model seeds the per-session config.toml model."""

    def test_replaces_top_level_model_only(self, tmp_path: Path) -> None:
        """The top-level ``model`` line is replaced; lookalike keys survive.

        ``model_provider`` / ``model_reasoning_effort`` also start with
        "model", and keys inside tables must never be touched — both were
        plausible regressions for a line-match implementation.
        """
        from omnigent.codex_native_app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-5.5"\n'
            'model_provider = "Databricks"\n'
            'model_reasoning_effort = "xhigh"\n'
            "[profiles.default]\n"
            'model = "table-scoped-stays"\n',
            encoding="utf-8",
        )
        _pin_codex_config_model(tmp_path, "databricks-gpt-5-4-mini")
        text = config.read_text(encoding="utf-8")
        assert 'model = "databricks-gpt-5-4-mini"' in text.splitlines()[0]
        assert 'model_provider = "Databricks"' in text
        assert 'model_reasoning_effort = "xhigh"' in text
        assert 'model = "table-scoped-stays"' in text
        assert "gpt-5.5" not in text

    def test_inserts_model_when_absent(self, tmp_path: Path) -> None:
        """A config with no top-level ``model`` gains one as the first line."""
        from omnigent.codex_native_app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text("[profiles.default]\nx = 1\n", encoding="utf-8")
        _pin_codex_config_model(tmp_path, "gpt-5.5")
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[0] == 'model = "gpt-5.5"'
        assert "[profiles.default]" in lines

    def test_materializes_symlink_without_touching_source(self, tmp_path: Path) -> None:
        """A symlinked config.toml is copied per-session; the shared source
        keeps its own model line (the live-caught clobber scenario)."""
        from omnigent.codex_native_app_server import _pin_codex_config_model

        shared = tmp_path / "shared-config.toml"
        shared.write_text('model = "gpt-5.5"\n', encoding="utf-8")
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").symlink_to(shared)
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert not (home / "config.toml").is_symlink()
        assert 'model = "databricks-gpt-5-4-mini"' in (home / "config.toml").read_text(
            encoding="utf-8"
        )
        assert shared.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'

    def test_read_back_by_forwarder_mirror_source(self, tmp_path: Path) -> None:
        """The forwarder's mirror source reads back exactly the pinned model.

        This is the regression the pin exists for: the mirror previously
        reported the shared file's stale model and overwrote the child's
        ``model_override``.
        """
        from omnigent.codex_native_app_server import _pin_codex_config_model
        from omnigent.codex_native_bridge import read_codex_config_model

        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        bridge_dir = tmp_path
        # read_codex_config_model resolves codex-home under the bridge dir.
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert read_codex_config_model(bridge_dir) == "databricks-gpt-5-4-mini"
