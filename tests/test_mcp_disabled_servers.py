"""Public contract for host-owned MCP disable state and launch planning."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.config import global_config_path
from omnigent.mcp_disabled_servers import (
    FILENAME,
    DisabledServerWriteError,
    McpStrictModeError,
    ServerKey,
    apply_claude_settings,
    claude_loaded_anyway,
    is_disabled,
    set_disabled,
)
from tests._helpers.mcp_disabled_servers import isolate_claude_settings, write_mcp_inheritance


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path / FILENAME


@pytest.fixture(autouse=True)
def claude_project(tmp_path, monkeypatch):
    return isolate_claude_settings(monkeypatch, tmp_path / "claude")


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_round_trip_exact_keys_no_pruning(config_home):
    keys = [
        ServerKey("claude", "github"),
        ServerKey("claude", "github", "tools"),
        ServerKey("codex", "missing"),
    ]
    for key in keys:
        set_disabled(key, True)
        set_disabled(key, True)
    assert config_home == global_config_path().parent / FILENAME
    assert json.loads(config_home.read_text()) == {
        "version": 1,
        "disabled": [
            {"harness": "claude", "server": "github"},
            {"harness": "claude", "server": "github", "plugin": "tools"},
            {"harness": "codex", "server": "missing"},
        ],
    }
    set_disabled(keys[0], False)
    assert not is_disabled(keys[0])
    assert all(is_disabled(key) for key in keys[1:])


@pytest.mark.parametrize(
    "key,code",
    [
        (ServerKey("other", "ok"), "unsupported_harness"),
        (ServerKey("claude", "omnigent"), "reserved_name"),
        (ServerKey("claude", ""), "invalid_name"),
        (ServerKey("codex", "ok", "plugin"), "invalid_name"),
        (ServerKey("claude", "ok", ""), "invalid_name"),
        (ServerKey("claude", "x" * 1025), "invalid_name"),
        (ServerKey("codex", "bad\ud800"), "invalid_name"),
        (ServerKey("claude", "ok", "bad\udfff"), "invalid_name"),
    ],
)
def test_invalid_writes(key, code):
    with pytest.raises(DisabledServerWriteError) as error:
        set_disabled(key, True)
    assert error.value.code == code
    assert not is_disabled(key)


@pytest.mark.parametrize(
    "content",
    [None, "{", "[]", '{"version":2,"disabled":[]}', " " * 65537, '{"version":1,"disabled":null}'],
    ids=[
        "missing",
        "malformed-json",
        "array-root",
        "unknown-version",
        "oversized",
        "invalid-list",
    ],
)
def test_bad_storage_fails_open_and_logs_once(config_home, content, caplog):
    if content is not None:
        config_home.write_text(content)
    for _ in range(2):
        assert not is_disabled(ServerKey("claude", "github"))
    assert len(caplog.records) == 1
    if content is None:
        set_disabled(ServerKey("claude", "github"), True)
        assert is_disabled(ServerKey("claude", "github"))
    else:
        with pytest.raises(DisabledServerWriteError) as error:
            set_disabled(ServerKey("claude", "github"), True)
        assert error.value.code == "io"
        assert config_home.read_bytes() == content.encode()
        assert len(caplog.records) == 1


def test_unreadable_storage_refuses_write_without_changing_bytes(config_home, monkeypatch):
    from pathlib import Path

    original = b'{"version":1,"disabled":[{"harness":"codex","server":"keep"}]}'
    config_home.write_bytes(original)
    real_open = Path.open

    def denied_open(path, *args, **kwargs):
        if path == config_home:
            raise PermissionError("unreadable")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied_open)
        assert not is_disabled(ServerKey("codex", "keep"))
        with pytest.raises(DisabledServerWriteError) as error:
            set_disabled(ServerKey("claude", "new"), True)
        assert error.value.code == "io"
    assert config_home.read_bytes() == original


def test_read_normalization(config_home):
    config_home.write_text(
        json.dumps(
            {
                "version": 1,
                "disabled": [
                    None,
                    {},
                    {"harness": "other", "server": "github"},
                    {"harness": "claude", "server": "omnigent"},
                    {"harness": "claude", "server": "github"},
                    {"harness": "claude", "server": "github"},
                ],
            }
        )
    )
    assert apply_claude_settings({}, executor="claude-native", active_harnesses=["claude"]) == {
        "deniedMcpServers": [{"serverName": "github"}]
    }


@pytest.mark.parametrize(
    "executor,settings",
    [
        ("claude-native", {"other": True}),
        ("claude-sdk", ' {"other":true} '),
        ("claude-sdk", None),
    ],
)
@pytest.mark.parametrize("active", [None, [], ["codex"], ["claude"]])
def test_claude_identity_without_applicable_entries(executor, settings, active):
    assert apply_claude_settings(settings, executor=executor, active_harnesses=active) is settings


@pytest.mark.parametrize("executor", ["claude-native", "claude-sdk"])
def test_claude_merge_order_plugins_and_identity(executor):
    set_disabled(ServerKey("claude", "github"), True)
    set_disabled(ServerKey("claude", "notion", "tools"), True)
    original = {
        "deniedMcpServers": [{"serverName": "existing"}, {"serverName": "github"}],
        "other": [1],
    }
    settings = original if executor == "claude-native" else json.dumps(original)
    assert apply_claude_settings(settings, executor=executor, active_harnesses=None) is settings
    result = apply_claude_settings(settings, executor=executor, active_harnesses=["claude"])
    parsed = result if isinstance(result, dict) else json.loads(result)
    assert parsed == {
        **original,
        "deniedMcpServers": original["deniedMcpServers"] + [{"serverName": "plugin:tools:notion"}],
    }
    assert len(original["deniedMcpServers"]) == 2
    assert apply_claude_settings(result, executor=executor, active_harnesses=["claude"]) is result
    assert claude_loaded_anyway(
        ["github", "notion", "plugin:tools:notion", "omnigent"], active_harnesses=["claude"]
    ) == ("github", "plugin:tools:notion")
    assert claude_loaded_anyway(["github"], active_harnesses=None) == ()


@pytest.mark.parametrize("settings", ["{", "[]", '"string"', "null"])
def test_malformed_sdk_settings_fail_open(settings):
    set_disabled(ServerKey("claude", "github"), True)
    assert (
        apply_claude_settings(settings, executor="claude-sdk", active_harnesses=["claude"])
        is settings
    )


@pytest.mark.parametrize("executor", ["claude-native", "claude-sdk"])
@pytest.mark.parametrize("active", [None, ["claude"]])
def test_claude_strict_allowlist_replaces_caller_allowlist(executor, active):
    write_mcp_inheritance({"inherit": False, "allow": ["zeta", "alpha", "omnigent"]})
    set_disabled(ServerKey("claude", "github"), True)
    original = {"allowedMcpServers": [{"serverName": "caller"}], "other": [1]}
    settings = original if executor == "claude-native" else json.dumps(original)
    result = apply_claude_settings(settings, executor=executor, active_harnesses=active)
    parsed = result if isinstance(result, dict) else json.loads(result)
    expected = {
        "other": [1],
        "allowedMcpServers": [
            {"serverName": "omnigent"},
            {"serverName": "alpha"},
            {"serverName": "zeta"},
        ],
    }
    if active:
        expected["deniedMcpServers"] = [{"serverName": "github"}]
    assert parsed == expected
    assert original["allowedMcpServers"] == [{"serverName": "caller"}]
    assert apply_claude_settings(result, executor=executor, active_harnesses=active) is result


def test_claude_strict_starts_from_empty_sdk_settings():
    write_mcp_inheritance({"inherit": False})
    result = apply_claude_settings(None, executor="claude-sdk", active_harnesses=None)
    assert json.loads(result) == {"allowedMcpServers": [{"serverName": "omnigent"}]}


def test_claude_strict_follows_executor_override():
    write_mcp_inheritance({"inherit": False})
    path = global_config_path()
    path.write_text(
        path.read_text() + "harness:\n  claude-native:\n    mcp:\n      inherit: true\n"
    )
    assert apply_claude_settings({}, executor="claude-native", active_harnesses=None) == {}
    assert "allowedMcpServers" in json.loads(
        apply_claude_settings("{}", executor="claude-sdk", active_harnesses=None)
    )


def test_claude_loaded_anyway_reports_names_outside_strict_allowlist():
    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    set_disabled(ServerKey("claude", "kept"), True)
    assert claude_loaded_anyway(
        ["omnigent", "kept", "extra", "plugin:tools:notion", "extra"], active_harnesses=["claude"]
    ) == ("kept", "extra", "plugin:tools:notion")


@pytest.mark.parametrize("settings", ["[]", '{"deniedMcpServers": {}}'])
def test_malformed_sdk_settings_refuse_strict_launch(settings, caplog):
    write_mcp_inheritance({"inherit": False})
    with pytest.raises(McpStrictModeError, match="claude-sdk launch"):
        apply_claude_settings(settings, executor="claude-sdk", active_harnesses=None)
    assert "mcp_strict_launch_refused executor=claude-sdk path=--settings" in caplog.text


@pytest.mark.parametrize("executor", ["claude-native", "claude-sdk"])
def test_claude_strict_denies_inherited_allow_entries(executor, tmp_path, claude_project):
    """Claude unions allowlists across sources, so inherited entries must become denies."""
    from omnigent.onboarding import ambient

    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    url = {"serverUrl": "https://mcp.example.com/*"}
    _write_json(
        tmp_path / "claude/claude-config/settings.json",
        {
            "allowedMcpServers": [
                {"serverName": "user_only"},
                {"serverName": "kept"},
                {"serverName": "omnigent"},
                url,
                "not-an-entry",
                {"serverName": 3, "serverCommand": "not-a-list"},
            ]
        },
    )
    _write_json(
        claude_project / ".claude/settings.json",
        {"allowedMcpServers": [{"serverName": "user_only"}, {"serverName": "project_only"}]},
    )
    _write_json(
        claude_project / ".claude/settings.local.json",
        {"allowedMcpServers": [{"serverName": "local_only"}]},
    )
    managed = ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS[0]
    _write_json(managed, {"allowedMcpServers": [{"serverName": "managed_only"}]})
    # Servers in managed-mcp.json skip the allowlist, so only a deny stops them.
    _write_json(
        managed.with_name("managed-mcp.json"),
        {"mcpServers": {"omnigent": {}, "kept": {}, "pinned": {"command": "pinned"}}},
    )
    _write_json(
        managed.with_name("managed-settings.d") / "10-team.json",
        {"allowedMcpServers": [{"serverName": "drop_in_only"}]},
    )
    # Claude versions read project settings from the launch directory or the Git root.
    _write_json(
        claude_project / "sub/.claude/settings.json",
        {"allowedMcpServers": [{"serverName": "subdirectory_only"}]},
    )
    original = {"deniedMcpServers": [{"serverName": "caller"}]}
    settings = original if executor == "claude-native" else json.dumps(original)
    result = apply_claude_settings(
        settings, executor=executor, active_harnesses=None, cwd=claude_project / "sub"
    )
    parsed = result if isinstance(result, dict) else json.loads(result)
    assert parsed == {
        "deniedMcpServers": [
            {"serverName": "caller"},
            {"serverName": "user_only"},
            url,
            {"serverName": "subdirectory_only"},
            {"serverName": "project_only"},
            {"serverName": "local_only"},
            {"serverName": "managed_only"},
            {"serverName": "drop_in_only"},
            {"serverName": "pinned"},
        ],
        "allowedMcpServers": [{"serverName": "omnigent"}, {"serverName": "kept"}],
    }


@pytest.mark.parametrize("main_local", ["readable", "unreadable"])
def test_claude_strict_reads_linked_worktree_and_main_checkout_settings(
    claude_project, main_local
):
    write_mcp_inheritance({"inherit": False})
    worktree = claude_project.parent / "worktree"
    metadata = claude_project / ".git/worktrees/session"
    _write_json(metadata / "commondir", "../..\n")
    _write_json(metadata / "gitdir", str(worktree / ".git"))
    _write_json(worktree / ".git", f"gitdir: {metadata}\n")
    _write_json(
        worktree / ".claude/settings.local.json",
        {"allowedMcpServers": [{"serverName": "worktree_local"}]},
    )
    main_settings = claude_project / ".claude/settings.local.json"
    if main_local == "unreadable":
        main_settings.mkdir(parents=True)
        with pytest.raises(McpStrictModeError) as raised:
            apply_claude_settings(
                {}, executor="claude-native", active_harnesses=None, cwd=worktree
            )
        assert raised.value.path == main_settings
        return
    _write_json(main_settings, {"allowedMcpServers": [{"serverName": "main_local"}]})
    result = apply_claude_settings(
        {}, executor="claude-native", active_harnesses=None, cwd=worktree
    )
    assert result["deniedMcpServers"] == [
        {"serverName": "worktree_local"},
        {"serverName": "main_local"},
    ]


@pytest.mark.parametrize("executor", ["claude-native", "claude-sdk"])
def test_claude_strict_refuses_inherited_command_entries(claude_project, executor):
    """Claude then admits stdio servers by command alone, ignoring the name allowlist."""
    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    # Both servers satisfy the command entry, so neither keeping nor denying it works.
    _write_json(
        claude_project.parent / "claude-config/.claude.json",
        {
            "mcpServers": {
                "kept": {"command": "npx", "args": ["server"], "env": {"ROLE": "kept"}},
                "other": {"command": "npx", "args": ["server"], "env": {"ROLE": "other"}},
            }
        },
    )
    settings = claude_project / ".claude/settings.json"
    _write_json(settings, {"allowedMcpServers": [{"serverCommand": ["npx", "server"]}]})
    with pytest.raises(McpStrictModeError, match="including for the omnigent relay") as raised:
        apply_claude_settings(
            {} if executor == "claude-native" else None, executor=executor, active_harnesses=None
        )
    assert raised.value.path == settings


@pytest.mark.parametrize(
    ("pattern", "source"),
    [
        ("https://mcp.example.com/kept", "user"),
        ("https://mcp.example.com/kept", "project_key"),
        ("https://mcp.example.com/*", "mcp_json"),
    ],
)
def test_claude_strict_refuses_url_entries_matching_allowlisted_servers(
    claude_project, pattern, source
):
    """Denying such an entry blocks the server; keeping it admits others at that URL."""
    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    servers = {"mcpServers": {"kept": {"type": "http", "url": "https://mcp.example.com/kept"}}}
    if source == "mcp_json":
        _write_json(claude_project / ".mcp.json", servers)
    else:
        # Claude keys projects by the physical path of its working directory.
        user = {"projects": {str(claude_project.resolve()): servers}}
        _write_json(
            claude_project.parent / "claude-config/.claude.json",
            servers if source == "user" else user,
        )
    other = {"serverUrl": "https://other.example.com/mcp"}
    settings = claude_project / ".claude/settings.json"
    _write_json(settings, {"allowedMcpServers": [other, {"serverUrl": pattern}]})
    launch = claude_project.parent / "linked-project"
    launch.symlink_to(claude_project, target_is_directory=True)
    with pytest.raises(
        McpStrictModeError,
        match=re.escape(f"serverUrl {pattern}, which matches allowlisted server kept"),
    ) as raised:
        apply_claude_settings({}, executor="claude-native", active_harnesses=None, cwd=launch)
    assert raised.value.path == launch / ".claude/settings.json"
    _write_json(settings, {"allowedMcpServers": [other]})
    result = apply_claude_settings({}, executor="claude-native", active_harnesses=None, cwd=launch)
    assert result["deniedMcpServers"] == [other]


def test_claude_strict_logs_unreadable_server_configs(claude_project, caplog):
    """An unreadable ``.mcp.json`` hides allowlisted transports, so it is logged."""
    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    (claude_project / ".mcp.json").write_text("{not json", encoding="utf-8")
    entry = {"serverUrl": "https://mcp.example.com/kept"}
    _write_json(claude_project / ".claude/settings.json", {"allowedMcpServers": [entry]})
    result = apply_claude_settings(
        {}, executor="claude-native", active_harnesses=None, cwd=claude_project
    )
    assert result["deniedMcpServers"] == [entry]
    assert (
        f"mcp_strict_claude_config_read_failed path={claude_project / '.mcp.json'} "
        "reason=JSONDecodeError"
    ) in caplog.text


def test_claude_strict_reads_settings_for_the_launch_environment(tmp_path, claude_project):
    """A relative config directory resolves against the launch cwd, not this process's."""
    write_mcp_inheritance({"inherit": False, "allow": ["kept"]})
    _write_json(
        tmp_path / "claude/claude-config/settings.json",
        {"allowedMcpServers": [{"serverName": "process_user"}]},
    )
    launch = tmp_path / "launch"
    launch_settings = launch / "launch-config/settings.json"
    _write_json(launch_settings, {"allowedMcpServers": [{"serverName": "launch_user"}]})
    env = {"CLAUDE_CONFIG_DIR": "launch-config"}
    result = apply_claude_settings(
        {}, executor="claude-native", active_harnesses=None, cwd=launch, env=env
    )
    assert result["deniedMcpServers"] == [{"serverName": "launch_user"}]
    # The launch's own global config supplies the allowlisted server's transport.
    url = "https://mcp.example.com/kept"
    _write_json(
        launch / "launch-config/.claude.json",
        {"mcpServers": {"kept": {"type": "http", "url": url}}},
    )
    _write_json(launch_settings, {"allowedMcpServers": [{"serverUrl": url}]})
    with pytest.raises(McpStrictModeError, match="matches allowlisted server kept"):
        apply_claude_settings(
            {}, executor="claude-native", active_harnesses=None, cwd=launch, env=env
        )


@pytest.mark.parametrize("content", ["{", "[]", None])
def test_unreadable_claude_settings_refuse_only_strict_launches(content, claude_project, caplog):
    path = claude_project / ".claude/settings.local.json"
    if content is None:
        path.mkdir(parents=True)  # Read failure, even when tests run as root.
    else:
        _write_json(path, content)
    set_disabled(ServerKey("claude", "github"), True)
    assert apply_claude_settings({}, executor="claude-native", active_harnesses=["claude"]) == {
        "deniedMcpServers": [{"serverName": "github"}]
    }
    write_mcp_inheritance({"inherit": False})
    with pytest.raises(McpStrictModeError) as refused:
        apply_claude_settings({}, executor="claude-native", active_harnesses=None)
    assert refused.value.path == path
    assert "harness.claude-native.mcp.inherit: true" in str(refused.value)
    assert f"mcp_strict_launch_refused executor=claude-native path={path}" in caplog.text


def test_thread_writers_do_not_lose_updates():
    keys = [ServerKey("claude", f"server_{index}") for index in range(30)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda key: set_disabled(key, True), keys))
    assert all(is_disabled(key) for key in keys)


def _process_write(config_home, barrier, index):
    import os

    os.environ["OMNIGENT_CONFIG_HOME"] = config_home
    barrier.wait(timeout=10)
    for suffix in range(5):
        set_disabled(ServerKey("codex", f"process_{index}_{suffix}"), True)


def test_posix_writers_do_not_lose_updates(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(6)
    processes = [
        context.Process(target=_process_write, args=(str(tmp_path), barrier, index))
        for index in range(6)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert all(
            is_disabled(ServerKey("codex", f"process_{index}_{suffix}"))
            for index in range(6)
            for suffix in range(5)
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()


def test_failed_replace_preserves_previous_file(config_home, monkeypatch):
    from omnigent import mcp_disabled_servers

    set_disabled(ServerKey("claude", "keep"), True)
    before = config_home.read_bytes()

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(mcp_disabled_servers.os, "replace", fail_replace)
    with pytest.raises(DisabledServerWriteError) as error:
        set_disabled(ServerKey("claude", "new"), True)
    assert error.value.code == "io"
    assert config_home.read_bytes() == before
    assert not list(config_home.parent.glob(f".{FILENAME}.*"))


def test_unreadable_storage(config_home):
    config_home.mkdir()
    assert not is_disabled(ServerKey("codex", "github"))
    with pytest.raises(DisabledServerWriteError) as error:
        set_disabled(ServerKey("codex", "github"), True)
    assert error.value.code == "io"


def test_default_config_home(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("OMNIGENT_CONFIG_HOME", None)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.mcp_disabled_servers import ServerKey, set_disabled; "
            'set_disabled(ServerKey("claude", "fixture"), True)',
        ],
        env=env,
        check=True,
    )
    assert json.loads((tmp_path / ".omnigent" / FILENAME).read_text())["disabled"] == [
        {"harness": "claude", "server": "fixture"}
    ]


def test_storage_size_boundary_and_write_refusal(config_home, caplog):
    content = json.dumps({"version": 1, "disabled": [{"harness": "claude", "server": "fixture"}]})
    config_home.write_text(content + " " * (65536 - len(content)))
    assert is_disabled(ServerKey("claude", "fixture"))
    config_home.write_text(config_home.read_text() + " ")
    assert not is_disabled(ServerKey("claude", "fixture"))
    original = config_home.read_bytes()
    with pytest.raises(DisabledServerWriteError) as error:
        set_disabled(ServerKey("claude", "new"), True)
    assert error.value.code == "io"
    assert config_home.read_bytes() == original
    assert "mcp_disabled_servers_replacing_invalid_storage" not in caplog.text


@pytest.mark.parametrize(
    "entry",
    [
        {"harness": "claude", "server": None},
        {"harness": [], "server": "fixture"},
        {"harness": "claude", "server": "fixture", "plugin": []},
    ],
)
def test_malformed_entry_is_ignored(config_home, entry):
    config_home.write_text(json.dumps({"version": 1, "disabled": [entry]}))
    settings = {}
    assert (
        apply_claude_settings(settings, executor="claude-native", active_harnesses=["claude"])
        is settings
    )


def test_concurrent_missing_reads_log_once(caplog):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: is_disabled(ServerKey("claude", "fixture")), range(40)))
    assert not any(results)
    assert len(caplog.records) == 1
