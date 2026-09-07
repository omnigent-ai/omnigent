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
    CODEX_EXTENDED_CATALOG_ENV_VAR,
    CODEX_ROUTER_DIR_ENV_VAR,
    CODEX_ROUTER_SESSION_ID_ENV_VAR,
    _CodexAppServerSession,
    _populate_codex_home_config,
    codex_extended_catalog_requested,
    codex_router_bridge_dir,
    codex_router_hooks_settings,
    codex_router_session_id,
    merge_codex_user_hooks,
    write_codex_router_hooks_file,
)
from omnigent.inner.hook_scripts.subagent_router import REQUEST_TIMEOUT_S

_USER_HOOKS = {
    "hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "user-pre"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}],
    }
}

# Enough of a ``codex debug models`` payload for ``extended_model_catalog`` to
# clone the GLM arm from: it needs the clone-source slug to be present.
_MODEL_CATALOG: dict[str, Any] = {  # type: ignore[explicit-any]
    "models": [
        {"slug": "gpt-5.6-luna", "visibility": "list", "supported_reasoning_levels": []},
    ]
}


def _write_user_home(tmp_path: Path, *, hooks: dict[str, object] | None = None) -> Path:
    source = tmp_path / "user-codex"
    source.mkdir()
    (source / "auth.json").write_text("{}")
    (source / "config.toml").write_text('model = "gpt-5.4-mini"\n')
    if hooks is not None:
        (source / "hooks.json").write_text(json.dumps(hooks))
    return source


def test_router_hooks_settings_registers_the_pretooluse_gate(tmp_path: Path) -> None:
    payload = codex_router_hooks_settings(
        tmp_path / "bridge",
        session_id="conv_abc",
        python_executable="/usr/bin/python3",
    )

    hooks = payload["hooks"]
    assert set(hooks) == {"PreToolUse"}
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


def test_populate_skips_hooks_symlink_when_hooks_are_injected(tmp_path: Path) -> None:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    codex_home = tmp_path / "private"
    codex_home.mkdir()

    _populate_codex_home_config(codex_home, source, inject_hooks=True)

    assert not (codex_home / "hooks.json").exists()
    assert (codex_home / "auth.json").is_symlink()
    assert (codex_home / "config.toml").is_file()


def test_populate_symlinks_hooks_when_none_are_injected(tmp_path: Path) -> None:
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


def test_the_launch_signals_survive_the_codex_env_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor reads its session class out of the FILTERED env.

    ``_clean_codex_env`` is both the codex subprocess env and the executor's
    own view of its launch, so a signal missing from its allowlist is dropped
    and the feature it gates silently never engages — the routing hooks and the
    extended catalog were both unreachable on the wrapped ``codex`` harness.
    """
    monkeypatch.setenv(CODEX_ROUTER_DIR_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(CODEX_ROUTER_SESSION_ID_ENV_VAR, "conv_abc")
    monkeypatch.setenv(CODEX_EXTENDED_CATALOG_ENV_VAR, "1")

    filtered = codex_executor._clean_codex_env()

    assert codex_router_bridge_dir(filtered) == tmp_path
    assert codex_router_session_id(filtered) == "conv_abc"
    assert codex_extended_catalog_requested(filtered) is True


def test_router_env_discovery(tmp_path: Path) -> None:
    env = {
        CODEX_ROUTER_DIR_ENV_VAR: str(tmp_path),
        CODEX_ROUTER_SESSION_ID_ENV_VAR: " conv_abc ",
    }

    assert codex_router_bridge_dir(env) == tmp_path
    assert codex_router_session_id(env) == "conv_abc"
    assert codex_router_bridge_dir({}) is None
    assert codex_router_session_id({}) is None


class _HomeSnapshot:
    """The private ``CODEX_HOME`` as codex would have read it at launch."""

    def __init__(self, home: Path) -> None:
        hooks = home / "hooks.json"
        self.is_symlink = hooks.is_symlink()
        self.payload: dict[str, Any] | None = None  # type: ignore[explicit-any]
        if hooks.is_file():
            self.payload = json.loads(hooks.read_text())
        self.catalog_written = (home / "model_catalog.json").is_file()
        config = home / "config.toml"
        self.config_names_catalog = config.is_file() and "model_catalog_json" in config.read_text(
            encoding="utf-8"
        )


class _FakeVersionProc:
    """Answers the routing gate's ``codex --version`` probe."""

    def __init__(self, version: str) -> None:
        self._stdout = f"codex-cli {version}\n".encode()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def _start_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str],
    probes: list[str] | None = None,
    codex_version: str = "0.145.0",
) -> tuple[tuple[str, ...], _HomeSnapshot]:
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    workspace = tmp_path / "work"
    workspace.mkdir()
    captured: list[tuple[tuple[str, ...], _HomeSnapshot]] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:  # type: ignore[explicit-any]
        if argv[1:2] == ("--version",):
            return _FakeVersionProc(codex_version)
        # The session deletes its private CODEX_HOME on the launch failure
        # below, so snapshot the home while codex would have read it.
        home = Path(kwargs["env"]["CODEX_HOME"])
        captured.append((argv, _HomeSnapshot(home)))
        raise RuntimeError("stop")

    def fake_probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any]:  # type: ignore[explicit-any]
        del source_home, timeout
        if probes is not None:
            probes.append(codex_path)
        return _MODEL_CATALOG

    monkeypatch.setattr(codex_executor, "populate_codex_skills_from_bundle", lambda *a, **k: None)
    monkeypatch.setattr(codex_executor, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(codex_executor, "_create_subprocess_exec", fake_exec)
    # Never shell out to a real ``codex debug models`` from a unit test, and
    # never let one session's cached catalog answer another's probe count.
    monkeypatch.setattr(codex_executor, "_find_codex_cli", lambda: "/bin/codex")
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", fake_probe)
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


# ── The three codex session classes (SDK arm) ───────────────────────
#
# The wrapped app-server harness, not the native terminal: its spawns go
# through the session-create path, which routes off the stamped switch with no
# in-harness gate, so this arm still takes auto-harness before it is armed.
# (The native arm arms a pinned Smart Routing session too — see
# ``tests/test_codex_native_app_server.py``.)
#
# A plain codex session must look exactly like a pre-Smart-Routing one: no
# ``codex debug models`` probe replacing its model catalog, no generated
# ``hooks.json`` (so its ``spawn_agent`` never waits on a routing gate), and
# the user's own hooks.json still symlinked so a mid-session edit takes effect.
# A pinned Smart Routing session adds the catalog and nothing else — its routed
# turn can land on a gateway arm codex's bundled catalog has no entry for. Only
# an auto-harness session, whose spawns the router may move across families,
# gets the spawn-routing hook too.


def test_a_plain_codex_session_gets_no_probe_and_keeps_its_hooks_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: list[str] = []

    _argv, home = _start_app_server(tmp_path, monkeypatch, env={}, probes=probes)

    assert probes == []
    assert not home.catalog_written
    assert not home.config_names_catalog
    assert home.is_symlink
    # The file codex reads is the user's own, byte for byte.
    assert home.payload == _USER_HOOKS


def test_a_pinned_smart_routing_codex_session_gets_the_catalog_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: list[str] = []

    _argv, home = _start_app_server(
        tmp_path,
        monkeypatch,
        env={CODEX_EXTENDED_CATALOG_ENV_VAR: "1"},
        probes=probes,
    )

    assert probes == ["/bin/codex"]
    assert home.catalog_written
    assert home.config_names_catalog
    # No hooks were injected, so the user's file is still the live one.
    assert home.is_symlink
    assert home.payload == _USER_HOOKS


def test_an_auto_harness_codex_session_gets_the_catalog_and_the_spawn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    probes: list[str] = []

    _argv, home = _start_app_server(
        tmp_path,
        monkeypatch,
        env={
            CODEX_EXTENDED_CATALOG_ENV_VAR: "1",
            CODEX_ROUTER_DIR_ENV_VAR: str(bridge),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
        probes=probes,
    )

    assert probes == ["/bin/codex"]
    assert home.catalog_written
    assert home.config_names_catalog
    assert not home.is_symlink
    assert home.payload is not None
    matchers = [entry.get("matcher") for entry in home.payload["hooks"]["PreToolUse"]]
    assert matchers == [r".*spawn_agent", None]


# ── The routing hook's own CLI floor ────────────────────────────────
#
# The spawn gate matches a flattened ``spawn_agent`` tool name codex only
# spells that way from 0.145.0 on. Below that the hook can never fire, so
# registering it would leave the session paying for a gate that does nothing
# *and* replace the user's symlinked hooks.json. The floor lives here rather
# than in the launch check so an older codex still launches.


def test_an_old_codex_cli_registers_no_spawn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()

    with caplog.at_level("WARNING"):
        _argv, home = _start_app_server(
            tmp_path,
            monkeypatch,
            env={
                CODEX_ROUTER_DIR_ENV_VAR: str(bridge),
                CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
            },
            codex_version="0.139.0",
        )

    # Routing no-ops: the user's own hooks file is still the live one.
    assert home.is_symlink
    assert home.payload == _USER_HOOKS
    assert "smart routing spawn gate disabled" in caplog.text


def test_a_new_enough_codex_cli_registers_the_spawn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()

    _argv, home = _start_app_server(
        tmp_path,
        monkeypatch,
        env={
            CODEX_ROUTER_DIR_ENV_VAR: str(bridge),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
        codex_version="0.145.0",
    )

    assert not home.is_symlink
    assert home.payload is not None
    matchers = [entry.get("matcher") for entry in home.payload["hooks"]["PreToolUse"]]
    assert r".*spawn_agent" in matchers


@pytest.mark.parametrize(
    ("version", "skipped"),
    [
        ((0, 144, 9), True),
        ((0, 145, 0), False),
        ((0, 146, 0), False),
        # An unparseable probe must not silently drop routing.
        (None, False),
    ],
)
def test_the_routing_hook_floor_reads_the_probed_version(
    version: tuple[int, int, int] | None, skipped: bool
) -> None:
    reason = codex_executor.codex_routing_hook_skip_reason(version)
    assert (reason is not None) is skipped
    if reason is not None:
        assert "smart routing spawn gate disabled" in reason


def test_an_old_codex_cli_keeps_the_sdk_harness_hooks_symlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapped (SDK) codex harness shares the floor with the native one."""
    source = _write_user_home(tmp_path, hooks=_USER_HOOKS)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    seen: list[_HomeSnapshot] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> Any:  # type: ignore[explicit-any]
        if argv[1:2] == ("--version",):
            return _FakeVersionProc("0.139.0")
        seen.append(_HomeSnapshot(Path(kwargs["env"]["CODEX_HOME"])))
        raise RuntimeError("stop")

    monkeypatch.setattr(codex_executor, "populate_codex_skills_from_bundle", lambda *a, **k: None)
    monkeypatch.setattr(codex_executor, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(codex_executor, "_create_subprocess_exec", fake_exec)
    session = codex_executor._CodexAppServerSession(
        codex_path="/bin/echo",
        cwd=str(workspace),
        env={
            CODEX_ROUTER_DIR_ENV_VAR: str(bridge),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
        tool_executor=None,
    )
    with contextlib.suppress(RuntimeError):
        asyncio.run(session.start())

    assert seen, "the app-server was never launched"
    assert seen[0].is_symlink
    assert seen[0].payload == _USER_HOOKS


def test_router_hook_commands_run_python_isolated(tmp_path: Path) -> None:
    """Every routing hook command passes ``-I`` before ``-m``.

    Codex runs hooks with the session's *workspace* as cwd, and ``-m``
    prepends cwd to ``sys.path``. A workspace holding a directory named
    ``omnigent`` — any checkout of this project, the most likely workspace
    of all — then shadows the installed package and the hook dies on
    ``ModuleNotFoundError``. Codex discards the failure, so the routing gate
    fails open in total silence. Observed live: a session whose cwd was a
    second omnigent checkout ran with the hook *trusted* but not working.
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
    """The routing hook runs when cwd holds a decoy ``omnigent`` package.

    The end-to-end guard for the isolation flag: runs the real generated
    ``route-subagent`` command from a workspace that shadows the installed
    package, exactly the live failure. Without ``-I`` the decoy package
    imports and the hook dies on the decoy's ``AssertionError``; with it the
    process starts cleanly, finds no router advertisement, and exits 0.
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
    command = hooks["PreToolUse"][0]["hooks"][0]["command"]

    result = subprocess.run(
        shlex.split(command),
        cwd=str(workspace),
        input="{}",
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "decoy package imported" not in result.stderr, result.stderr
