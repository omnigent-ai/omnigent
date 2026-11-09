"""Public contract for host-owned MCP disable state and launch planning."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.config import global_config_path
from omnigent.mcp_disabled_servers import (
    FILENAME,
    DisabledServerWriteError,
    ServerKey,
    apply_claude_settings,
    claude_loaded_anyway,
    is_disabled,
    plan_codex_launch,
    set_disabled,
)


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path / FILENAME


class FakeCodexProbe:
    def __init__(self, servers=None, error=None):
        self.servers = servers or {}
        self.error = error
        self.calls = []

    async def effective_servers(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return self.servers


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
    set_disabled(ServerKey("claude", "github"), True)
    assert is_disabled(ServerKey("claude", "github"))


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


@pytest.mark.parametrize("executor", ["codex", "codex-native"])
@pytest.mark.parametrize("active", [None, [], ["claude"], ["codex"]])
async def test_codex_no_probe_without_entries(executor, active, tmp_path):
    probe = FakeCodexProbe(error=AssertionError("must not probe"))
    plan = await plan_codex_launch(
        "codex",
        ["base=true"],
        executor=executor,
        active_harnesses=active,
        env={},
        cwd=tmp_path,
        probe=probe,
    )
    assert plan.config_overrides == plan.fallback_config_overrides == ("base=true",)
    assert plan.outcome == "none"
    assert plan.overrides_after_early_exit() is None
    assert not probe.calls


@pytest.mark.parametrize("executor", ["codex", "codex-native"])
async def test_codex_reported_names_only_and_probe_contract(executor, tmp_path):
    for name in ["github", "missing", "already_off"]:
        set_disabled(ServerKey("codex", name), True)
    probe = FakeCodexProbe({"github": True, "already_off": False})
    plan = await plan_codex_launch(
        "/bin/codex",
        ["base=true"],
        executor=executor,
        active_harnesses=["codex"],
        env={"CODEX_HOME": "private"},
        cwd=tmp_path,
        probe=probe,
    )
    assert probe.calls == [
        (
            ["/bin/codex", "-c", "base=true", "mcp", "list", "--json"],
            {"env": {"CODEX_HOME": "private"}, "cwd": tmp_path, "timeout_s": 3.0},
        )
    ]
    assert plan.config_overrides == (
        "base=true",
        "mcp_servers.github.enabled=false",
        "mcp_servers.already_off.enabled=false",
    )
    assert plan.outcome == "applied"
    assert plan.dropped == ("missing",)
    assert plan.overrides_after_early_exit() == ("base=true",)


@pytest.mark.parametrize("error", [TimeoutError(), OSError("no CLI"), ValueError("bad json")])
async def test_codex_probe_failure(error, tmp_path, caplog):
    set_disabled(ServerKey("codex", "github"), True)
    plan = await plan_codex_launch(
        "codex",
        ["base=true"],
        executor="codex",
        active_harnesses=["codex"],
        env={},
        cwd=tmp_path,
        probe=FakeCodexProbe(error=error),
    )
    assert plan.config_overrides == plan.fallback_config_overrides == ("base=true",)
    assert plan.outcome == "probe_failed"
    assert plan.overrides_after_early_exit() is None
    assert "mcp_disabled_preflight_failed" in caplog.text


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


async def test_codex_all_names_dropped(tmp_path):
    set_disabled(ServerKey("codex", "missing"), True)
    plan = await plan_codex_launch(
        "codex",
        [],
        executor="codex",
        active_harnesses=["codex"],
        env={},
        cwd=tmp_path,
        probe=FakeCodexProbe(),
    )
    assert plan.outcome == "none"
    assert plan.dropped == ("missing",)
    assert plan.overrides_after_early_exit() is None


async def test_codex_quoted_key(tmp_path):
    set_disabled(ServerKey("codex", 'name.with."quotes'), True)
    plan = await plan_codex_launch(
        "codex",
        [],
        executor="codex",
        active_harnesses=["codex"],
        env={},
        cwd=tmp_path,
        probe=FakeCodexProbe({'name.with."quotes': True}),
    )
    import tomllib

    assert tomllib.loads(plan.config_overrides[0]) == {
        "mcp_servers": {'name.with."quotes': {"enabled": False}}
    }


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


def test_storage_size_boundary_and_replacement_log(config_home, caplog):
    content = json.dumps({"version": 1, "disabled": [{"harness": "claude", "server": "fixture"}]})
    config_home.write_text(content + " " * (65536 - len(content)))
    assert is_disabled(ServerKey("claude", "fixture"))
    config_home.write_text(config_home.read_text() + " ")
    assert not is_disabled(ServerKey("claude", "fixture"))
    set_disabled(ServerKey("claude", "new"), True)
    assert "mcp_disabled_servers_replacing_invalid_storage" in caplog.text
    assert is_disabled(ServerKey("claude", "new"))


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
