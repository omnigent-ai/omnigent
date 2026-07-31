from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner import codex_executor
from omnigent.inner.codex_executor import (
    CODEX_ROUTER_DIR_ENV_VAR,
    CODEX_ROUTER_SESSION_ID_ENV_VAR,
    SUBAGENT_ROUTING_UNENFORCED_WARNING,
    _CodexAppServerSession,
    _populate_codex_home_config,
    codex_router_bridge_dir,
    codex_router_canary_fired,
    codex_router_hooks_settings,
    codex_router_session_id,
    merge_codex_user_hooks,
    read_codex_spawn_audit,
    reconcile_spawn_audit,
    subagent_routing_unenforced_warning,
    write_codex_router_hooks_file,
)
from omnigent.inner.hook_scripts.codex_router_hook import AUDIT_FILENAME, CANARY_FILENAME
from omnigent.inner.hook_scripts.subagent_router import REQUEST_TIMEOUT_S

_USER_HOOKS = {
    "hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "user-pre"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}],
    }
}


def _write_user_home(tmp_path: Path, *, hooks: dict[str, object] | None = None) -> Path:
    source = tmp_path / "user-codex"
    source.mkdir()
    (source / "auth.json").write_text("{}")
    (source / "config.toml").write_text('model = "gpt-5.4-mini"\n')
    if hooks is not None:
        (source / "hooks.json").write_text(json.dumps(hooks))
    return source


def test_router_hooks_settings_registers_three_events(tmp_path: Path) -> None:
    payload = codex_router_hooks_settings(
        tmp_path / "bridge",
        session_id="conv_abc",
        python_executable="/usr/bin/python3",
    )

    hooks = payload["hooks"]
    assert set(hooks) == {"PreToolUse", "SessionStart", "SubagentStart"}
    (pre_entry,) = hooks["PreToolUse"]
    # Regex, never the flattened literal ``collaborationspawn_agent``.
    assert pre_entry["matcher"] == r".*spawn_agent"
    (pre_hook,) = pre_entry["hooks"]
    assert pre_hook["type"] == "command"
    # Codex's kill is the outermost bound: just above the hook's own budget.
    assert pre_hook["timeout"] > REQUEST_TIMEOUT_S
    assert pre_hook["timeout"] < 2 * REQUEST_TIMEOUT_S
    assert "route-subagent" in pre_hook["command"]
    assert "--session-id conv_abc" in pre_hook["command"]
    assert "--harness codex" in pre_hook["command"]
    assert f"--bridge-dir {tmp_path / 'bridge'}" in pre_hook["command"]
    assert "session-canary" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "record-subagent" in hooks["SubagentStart"][0]["hooks"][0]["command"]
    assert "matcher" not in hooks["SessionStart"][0]


def test_router_hooks_settings_omits_session_flag_when_unknown(tmp_path: Path) -> None:
    payload = codex_router_hooks_settings(tmp_path, python_executable="/usr/bin/python3")

    assert "--session-id" not in payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_merge_user_hooks_preserves_user_entries_after_omnigent(tmp_path: Path) -> None:
    user_hooks = tmp_path / "hooks.json"
    user_hooks.write_text(json.dumps(_USER_HOOKS))
    payload = codex_router_hooks_settings(tmp_path, python_executable="/usr/bin/python3")

    merged = merge_codex_user_hooks(payload, user_hooks)

    pre = merged["hooks"]["PreToolUse"]
    assert len(pre) == 2
    assert pre[0]["matcher"] == r".*spawn_agent"
    assert pre[1]["hooks"][0]["command"] == "user-pre"
    assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "user-stop"
    # The original payload is not mutated.
    assert len(payload["hooks"]["PreToolUse"]) == 1


def test_merge_user_hooks_tolerates_malformed_user_file(tmp_path: Path) -> None:
    user_hooks = tmp_path / "hooks.json"
    user_hooks.write_text("{not json")
    payload = codex_router_hooks_settings(tmp_path, python_executable="/usr/bin/python3")

    assert merge_codex_user_hooks(payload, user_hooks) == payload


def test_write_router_hooks_file_replaces_symlink_and_merges(tmp_path: Path) -> None:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    codex_home = tmp_path / "private"
    codex_home.mkdir()
    _populate_codex_home_config(codex_home, source)
    assert (codex_home / "hooks.json").is_symlink()

    path = write_codex_router_hooks_file(
        codex_home,
        tmp_path / "bridge",
        session_id="conv_abc",
        python_executable="/usr/bin/python3",
    )

    assert not path.is_symlink()
    payload = json.loads(path.read_text())
    assert [entry.get("matcher") for entry in payload["hooks"]["PreToolUse"]] == [
        r".*spawn_agent",
        None,
    ]
    assert payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "user-stop"
    # The user's real hooks.json is untouched.
    assert json.loads((source / "hooks.json").read_text()) == _USER_HOOKS


def test_populate_skips_hooks_symlink_when_routing_on(tmp_path: Path) -> None:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    codex_home = tmp_path / "private"
    codex_home.mkdir()

    _populate_codex_home_config(codex_home, source, subagent_routing=True)

    assert not (codex_home / "hooks.json").exists()
    assert (codex_home / "auth.json").is_symlink()
    assert (codex_home / "config.toml").is_file()


def test_populate_symlinks_hooks_when_routing_off(tmp_path: Path) -> None:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    codex_home = tmp_path / "private"
    codex_home.mkdir()

    _populate_codex_home_config(codex_home, source)

    assert (codex_home / "hooks.json").is_symlink()
    assert (codex_home / "hooks.json").resolve() == (source / "hooks.json").resolve()


def test_write_router_hooks_file_without_user_hooks(tmp_path: Path) -> None:
    codex_home = tmp_path / "private"
    codex_home.mkdir()

    path = write_codex_router_hooks_file(
        codex_home,
        tmp_path / "bridge",
        user_hooks_source=tmp_path / "missing" / "hooks.json",
        python_executable="/usr/bin/python3",
    )

    payload = json.loads(path.read_text())
    assert len(payload["hooks"]["PreToolUse"]) == 1


def test_router_env_discovery(tmp_path: Path) -> None:
    env = {
        CODEX_ROUTER_DIR_ENV_VAR: str(tmp_path),
        CODEX_ROUTER_SESSION_ID_ENV_VAR: " conv_abc ",
    }

    assert codex_router_bridge_dir(env) == tmp_path
    assert codex_router_session_id(env) == "conv_abc"
    assert codex_router_bridge_dir({}) is None
    assert codex_router_session_id({}) is None


def test_canary_detection(tmp_path: Path) -> None:
    assert not codex_router_canary_fired(tmp_path)

    (tmp_path / CANARY_FILENAME).write_text(json.dumps({"session_id": "conv_abc"}))

    assert codex_router_canary_fired(tmp_path)


def test_read_spawn_audit_skips_malformed_lines(tmp_path: Path) -> None:
    assert read_codex_spawn_audit(tmp_path) == []
    (tmp_path / AUDIT_FILENAME).write_text(
        '{"agent_id": "a1", "model": "claude-sonnet-5"}\n'
        "not json\n"
        "\n"
        '{"agent_id": "a2", "model": "gpt-5-6-sol"}\n'
        "[1, 2]\n"
    )

    records = read_codex_spawn_audit(tmp_path)

    assert [(r["agent_id"], r["model"]) for r in records] == [
        ("a1", "claude-sonnet-5"),
        ("a2", "gpt-5-6-sol"),
    ]


class _HooksSnapshot:
    def __init__(self, path: Path) -> None:
        self.is_symlink = path.is_symlink()
        self.payload: dict[str, Any] | None = None  # type: ignore[explicit-any]
        if path.is_file():
            self.payload = json.loads(path.read_text())


def _start_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str],
) -> tuple[tuple[str, ...], _HooksSnapshot]:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    workspace = tmp_path / "work"
    workspace.mkdir()
    captured: list[tuple[tuple[str, ...], _HooksSnapshot]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> None:  # type: ignore[explicit-any]
        # The session deletes its private CODEX_HOME on the launch failure
        # below, so snapshot the hooks file while codex would have read it.
        home = Path(kwargs["env"]["CODEX_HOME"])
        captured.append((argv, _HooksSnapshot(home / "hooks.json")))
        raise RuntimeError("stop")

    monkeypatch.setattr(codex_executor, "populate_codex_skills_from_bundle", lambda *a, **k: None)
    monkeypatch.setattr(codex_executor, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(codex_executor, "_create_subprocess_exec", fake_exec)
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(workspace),
        env=env,
        tool_executor=None,
    )
    with contextlib.suppress(RuntimeError):
        asyncio.run(session.start())
    assert captured, "the app-server was never launched"
    return captured[0]


def test_app_server_argv_carries_no_hook_trust_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()

    argv, hooks = _start_app_server(
        tmp_path,
        monkeypatch,
        env={
            CODEX_ROUTER_DIR_ENV_VAR: str(bridge),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
    )

    assert argv[:2] == ("/bin/echo", "app-server")
    assert "--dangerously-bypass-hook-trust" not in argv
    assert hooks.payload is not None
    payload = hooks.payload
    assert len(payload["hooks"]["PreToolUse"]) == 2
    assert "--session-id conv_abc" in payload["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_app_server_keeps_symlinked_hooks_when_routing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, hooks = _start_app_server(tmp_path, monkeypatch, env={})

    assert argv[:2] == ("/bin/echo", "app-server")
    assert hooks.is_symlink


def test_unenforced_warning_payload() -> None:
    warning = subagent_routing_unenforced_warning("canary did not fire")

    assert warning == {
        "code": SUBAGENT_ROUTING_UNENFORCED_WARNING,
        "harness": "codex-native",
        "reason": "canary did not fire",
    }


def test_reconcile_spawn_audit_flags_a_model_the_router_never_approved() -> None:
    warnings = reconcile_spawn_audit(
        [
            {"agent_id": "a1", "task_name": "reviewer", "model": "gpt-5-6-luna"},
            {"agent_id": "a2", "task_name": "tester", "model": "glm-5-2"},
        ],
        [
            {"action": "rewrite", "model": "glm-5-2", "task_name": "reviewer"},
            {"action": "rewrite", "model": "glm-5-2", "task_name": "tester"},
        ],
    )

    assert len(warnings) == 1
    assert warnings[0]["code"] == SUBAGENT_ROUTING_UNENFORCED_WARNING
    assert "gpt-5-6-luna" in warnings[0]["reason"]
    assert "glm-5-2" in warnings[0]["reason"]


def test_reconcile_spawn_audit_accepts_a_different_spelling_of_a_routed_model() -> None:
    """Codex's own spelling of an approved model is not a violation.

    The audit reports whatever id codex ran with, which differs from the
    router's catalog id by prefix / case. Comparing raw strings posted an
    ``unenforced`` banner every 30s on a perfectly healthy session.
    """
    assert (
        reconcile_spawn_audit(
            [{"agent_id": "a1", "task_name": "reviewer", "model": "GPT-5-5"}],
            [{"action": "rewrite", "model": "databricks-gpt-5-5", "task_name": "reviewer"}],
        )
        == []
    )


def test_reconcile_spawn_audit_is_silent_without_routed_models() -> None:
    assert reconcile_spawn_audit([{"model": "gpt-5-6-luna"}], []) == []


def test_reconcile_spawn_audit_ignores_a_spawn_routing_was_toggled_off_for() -> None:
    """A session mixing routed and unrouted spawns must flag neither.

    Routing toggled off mid-session (or a router outage) relays a verdict
    that approves no model, so that spawn runs on the inherited model. The
    all-or-nothing compare flagged every such spawn as "unenforced".
    """
    warnings = reconcile_spawn_audit(
        [
            {"agent_id": "a1", "task_name": "routed", "model": "glm-5-2"},
            {"agent_id": "a2", "task_name": "unrouted", "model": "gpt-5-6-luna"},
        ],
        [
            {"action": "rewrite", "model": "glm-5-2", "task_name": "routed"},
            # Routing off for this spawn: relayed, but no model approved.
            {"action": "allow", "model": None, "task_name": "unrouted"},
        ],
    )
    assert warnings == []


def test_reconcile_spawn_audit_still_flags_the_routed_spawn_in_a_mixed_session() -> None:
    warnings = reconcile_spawn_audit(
        [{"agent_id": "a1", "task_name": "routed", "model": "gpt-5-6-luna"}],
        [
            {"action": "rewrite", "model": "glm-5-2", "task_name": "routed"},
            {"action": "allow", "model": None, "task_name": "unrouted"},
        ],
    )
    assert [w["reason"] for w in warnings] == [
        "spawned model gpt-5-6-luna != routed model glm-5-2"
    ]


def test_reconcile_spawn_audit_skips_unjoinable_records_in_a_mixed_session() -> None:
    """Without a task name, "unrouted" cannot be told from "ignored"."""
    assert (
        reconcile_spawn_audit(
            [{"agent_id": "a1", "model": "gpt-5-6-luna"}],
            [
                {"action": "rewrite", "model": "glm-5-2", "task_name": "routed"},
                {"action": "allow", "model": None, "task_name": "unrouted"},
            ],
        )
        == []
    )


def test_reconcile_spawn_audit_falls_back_session_wide_when_every_spawn_was_routed() -> None:
    warnings = reconcile_spawn_audit(
        [{"agent_id": "a1", "model": "gpt-5-6-luna"}],
        [{"action": "rewrite", "model": "glm-5-2", "task_name": "routed"}],
    )
    assert len(warnings) == 1


def test_router_hook_commands_run_python_isolated(tmp_path: Path) -> None:
    """Every routing hook command passes ``-I`` before ``-m``.

    Codex runs hooks with the session's *workspace* as cwd, and ``-m``
    prepends cwd to ``sys.path``. A workspace holding a directory named
    ``omnigent`` — any checkout of this project, the most likely workspace
    of all — then shadows the installed package and the hook dies on
    ``ModuleNotFoundError``. Codex discards the failure, so the routing gate
    fails open in total silence. Observed live: a session whose cwd was a
    second omnigent checkout ran with all three hooks *trusted* and none of
    them working.
    """
    hooks = codex_router_hooks_settings(
        tmp_path, session_id="conv_abc", python_executable="/venv/bin/python"
    )["hooks"]
    commands = [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]
    assert commands, "no routing hook commands generated"
    for command in commands:
        argv = shlex.split(command)
        assert argv[1:3] == ["-I", "-m"], f"expected isolated python in {command!r}"


def test_router_hook_survives_a_shadowing_workspace(tmp_path: Path) -> None:
    """The canary hook works when cwd holds a decoy ``omnigent`` package.

    The end-to-end guard for the isolation flag: runs the real generated
    command from a workspace that shadows the installed package, exactly
    the live failure. Without ``-I`` this exits non-zero and writes nothing.
    """
    workspace = tmp_path / "workspace"
    decoy = workspace / "omnigent"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("raise AssertionError('decoy package imported')\n")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hooks = codex_router_hooks_settings(
        bridge_dir, session_id="conv_abc", python_executable=sys.executable
    )["hooks"]
    command = hooks["SessionStart"][0]["hooks"][0]["command"]

    result = subprocess.run(
        shlex.split(command),
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert codex_router_canary_fired(bridge_dir), result.stderr
