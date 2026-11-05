"""Host catalogs preserve session scope without depending on a runner."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

from omnigent.claude_launcher import ClaudeLauncher
from omnigent.harnesses.claude_native import plugin_discovery
from omnigent.host.frames import HostSkillsFrame
from omnigent.host.skill_content import read_skill_content
from omnigent.host.skills import (
    INCOMPLETE_CATALOG_TTL_SECONDS,
    SKILLS_CACHE_TTL_SECONDS,
    HostSkillDiscovery,
)
from omnigent.spec import load
from omnigent.spec.skill_sources import resolve_session_skills
from tests._helpers.claude_launch import configure_claude_launch
from tests._helpers.polling import poll_until
from tests._helpers.providers import isolate_provider_resolution


def _skill(name: str, *, hidden: bool = False) -> str:
    return (
        f"---\nname: {name}\ndescription: {name} description\n"
        f"user-invocable: {str(not hidden).lower()}\n---\nPrivate instructions\n"
    )


def _bundle(files: dict[str, str], **config: object) -> bytes:
    config = {
        "spec_version": 1,
        "name": "test-agent",
        "llm": {"model": "test", "connection": {"api_key": "test-key"}},
        "executor": {"config": {"harness": "claude-sdk"}},
        **config,
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, text in {"config.yaml": yaml.safe_dump(config), **files}.items():
            content = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _response(bundle: bytes, version: str = "1") -> httpx.Response:
    return httpx.Response(
        200,
        content=bundle,
        headers={
            "X-Agent-Id": "agent",
            "X-Agent-Version": version,
            "X-Agent-Session-Scoped": "true",
        },
        request=httpx.Request("GET", "http://server/bundle"),
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.mark.parametrize(
    "skill_filter,expected",
    [("all", ["bundled", "local"]), ("none", ["bundled"]), (["local"], ["bundled", "local"])],
)
def test_session_catalog_matches_invocation_scope(
    tmp_path: Path, skill_filter: object, expected: list[str]
) -> None:
    for name in ("bundled", "hidden", "local"):
        directory = tmp_path / ".claude" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(_skill(name))
    bundle = _bundle(
        {
            "skills/bundled/SKILL.md": _skill("bundled"),
            "skills/hidden/SKILL.md": _skill("hidden", hidden=True),
        },
        skills=skill_filter,
    )
    fetch = Mock(return_value=_response(bundle))
    discovery = HostSkillDiscovery(fetch)
    frame = HostSkillsFrame("request", "session", str(tmp_path), "session", "agent", "1")
    catalog = discovery.discover(frame, tmp_path)
    assert [s["name"] for s in catalog] == expected
    assert all(set(s) == {"name", "description"} for s in catalog)

    runner_bundle = tmp_path / "runner-bundle"
    spec = load(bundle, dest=runner_bundle, expand_env=False)
    resolved = resolve_session_skills(spec, (tmp_path, runner_bundle), runner_bundle)
    assert catalog == [{"name": s.name, "description": s.description} for s in resolved]
    assert resolved[0].skill_dir == runner_bundle / "skills" / "bundled"


@pytest.mark.parametrize("remove_link", [True, False], ids=["removed", "permission-denied"])
def test_session_catalog_handles_host_skill_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remove_link: bool,
) -> None:
    skills_dir = Path.home() / ".codex" / "skills"
    local = skills_dir / "local"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text(_skill("local"))
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "SKILL.md").write_text(_skill("changing"))
    changing = skills_dir / "changing"
    changing.symlink_to(installed, target_is_directory=True)
    readlink = os.readlink

    def readlink_during_update(path: str, *, dir_fd: int | None = None) -> str:
        if path == str(changing):
            if not remove_link:
                raise PermissionError("synthetic access denial")
            # The provider already read SKILL.md; resolve has just seen a symlink.
            changing.unlink()
        return readlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", readlink_during_update)
    bundle = _bundle(
        {"skills/bundled/SKILL.md": _skill("bundled")},
        executor={"config": {"harness": "codex-native"}},
    )
    discovery = HostSkillDiscovery(lambda _: _response(bundle))
    frame = HostSkillsFrame("request", "session", str(tmp_path), "session", "agent", "1")
    if not remove_link:
        with pytest.raises(PermissionError, match="synthetic access denial"):
            discovery.discover(frame, tmp_path)
        return

    assert discovery.discover(frame, tmp_path) == [
        {"name": "bundled", "description": "bundled description"},
        {"name": "local", "description": "local description"},
    ]
    assert "Skipping host skill whose directory disappeared during discovery" in caplog.text
    assert str(changing) not in caplog.text


def test_nested_subagent_uses_directory_name_and_own_filter(tmp_path: Path) -> None:
    child_config = yaml.safe_dump(
        {
            "spec_version": 1,
            "name": "display-name",
            "llm": {"model": "test", "connection": {"api_key": "test"}},
            "skills": "none",
            "executor": {"config": {"harness": "claude-sdk"}},
        }
    )
    bundle = _bundle(
        {
            "skills/parent/SKILL.md": _skill("parent"),
            "agents/level/config.yaml": child_config.replace("display-name", "middle"),
            "agents/level/agents/child-dir/config.yaml": child_config,
            "agents/level/agents/child-dir/skills/child/SKILL.md": _skill("child"),
        }
    )
    discovery = HostSkillDiscovery(lambda _: _response(bundle))
    frame = HostSkillsFrame(
        "request", "session", str(tmp_path), "session", "agent", "1", "display-name"
    )
    assert discovery.discover(frame, tmp_path) == [
        {"name": "child", "description": "child description"}
    ]


@pytest.mark.parametrize(
    "harness,variable,subdir",
    [
        ("claude-native", "CLAUDE_CONFIG_DIR", "skills"),
        ("codex-native", "CODEX_HOME", "skills"),
    ],
)
def test_host_and_runner_discover_the_same_custom_config_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    variable: str,
    subdir: str,
) -> None:
    from omnigent.host.connect import _build_runner_env

    config_dir = tmp_path / "custom-config"
    directory = config_dir / subdir / "custom"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_skill("custom"))
    monkeypatch.setenv(variable, str(config_dir))
    expected_names = {"custom"}
    tiers = [(".agents", "portable")]
    if harness == "claude-native":
        tiers.append((".claude", "native"))
    for tier, name in tiers:
        skill = tmp_path / tier / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_skill(name))
        expected_names.add(name)
    discovery = HostSkillDiscovery(lambda _: pytest.fail("No bundle expected"))
    frame = HostSkillsFrame("request", harness, str(tmp_path))
    host_catalog = discovery.discover(frame, tmp_path)
    env = _build_runner_env(
        {variable: str(config_dir)},
        server_url="http://server",
        runner_id="runner",
        binding_token="token",
        workspace=str(tmp_path),
        parent_pid=1,
    )
    assert env[variable] == str(config_dir)
    monkeypatch.setenv(variable, env[variable])
    bundle = _bundle({}, executor={"config": {"harness": harness}})
    spec = load(bundle, dest=tmp_path / "runner-bundle", expand_env=False)
    resolved = resolve_session_skills(spec, (tmp_path,), None)
    assert host_catalog == [{"name": s.name, "description": s.description} for s in resolved]
    assert {s["name"] for s in host_catalog} == expected_names


def test_directory_cache_separates_agent_filters(tmp_path: Path) -> None:
    directory = tmp_path / ".claude" / "skills" / "local"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_skill("local"))
    discovery = HostSkillDiscovery(lambda _: pytest.fail("No bundle expected"))
    frame = HostSkillsFrame("request", "claude-sdk", str(tmp_path))
    assert discovery.discover(frame, tmp_path)
    assert discovery.discover(replace(frame, skills_filter="none"), tmp_path) == []
    assert discovery.discover(replace(frame, skills_filter=["local"]), tmp_path)


def test_host_cache_expires_and_keys_by_directory_and_agent_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr("omnigent.host.skills.time", SimpleNamespace(monotonic=lambda: now[0]))
    fetch = Mock(side_effect=lambda frame: _response(_bundle({}), frame.agent_version))
    discovery = HostSkillDiscovery(fetch)
    frame = HostSkillsFrame("request", "session", str(tmp_path), "session", "agent", "1")
    assert discovery.discover(frame, tmp_path) == []
    discovery.discover(frame, tmp_path)
    assert fetch.call_count == 1
    directory = tmp_path / ".claude/skills/new"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_skill("new"))
    assert discovery.discover(frame, tmp_path) == []
    now[0] = 61
    assert discovery.discover(frame, tmp_path)[0]["name"] == "new"
    discovery.discover(replace(frame, agent_version="2"), tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    discovery.discover(frame, other)
    assert fetch.call_count == 4


def test_failed_or_changed_bundle_is_not_cached(tmp_path: Path) -> None:
    fetch = Mock(
        side_effect=[
            httpx.ReadTimeout("unavailable"),
            _response(_bundle({}), "2"),
            _response(_bundle({})),
        ]
    )
    discovery = HostSkillDiscovery(fetch)
    frame = HostSkillsFrame("request", "session", str(tmp_path), "session", "agent", "1")
    with pytest.raises(httpx.ReadTimeout):
        discovery.discover(frame, tmp_path)
    with pytest.raises(ValueError, match="agent changed"):
        discovery.discover(frame, tmp_path)
    assert discovery.discover(frame, tmp_path) == []
    assert fetch.call_count == 3


def test_directory_catalog_never_downloads_a_session_bundle(tmp_path: Path) -> None:
    directory = tmp_path / ".claude/skills/local"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(_skill("local"))
    fetch = Mock()
    discovery = HostSkillDiscovery(fetch)
    assert discovery.discover(
        HostSkillsFrame("request", "claude-native", str(tmp_path)), tmp_path
    ) == [{"name": "local", "description": "local description"}]
    fetch.assert_not_called()


def test_incomplete_codex_catalog_reports_warnings_and_is_cached_only_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr("omnigent.host.skills.time", SimpleNamespace(monotonic=lambda: now[0]))
    codex_home = tmp_path / "home" / ".codex"
    for directory in (codex_home / "skills" / "local", codex_home / "skills" / "later"):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(_skill(directory.name))
    plugin = codex_home / "plugins" / "cache" / "market" / "toolkit" / "1.0.0"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "toolkit"}))
    (plugin / "skills" / "review").mkdir(parents=True)
    (plugin / "skills" / "review" / "SKILL.md").write_text(_skill("review"))
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: "/test/codex")
    cli = Mock(side_effect=subprocess.CalledProcessError(1, "codex", stderr="Error: broken"))
    monkeypatch.setattr("omnigent.spec.codex_plugin_skills.subprocess.run", cli)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.skill_discovery.loaded_codex_skills",
        Mock(side_effect=RuntimeError("Codex skills/list connection failed")),
    )
    discovery = HostSkillDiscovery(lambda _: pytest.fail("No bundle expected"))
    frame = HostSkillsFrame("request", "codex-native", str(tmp_path))

    def names(warnings: list[str]) -> list[str]:
        return [skill["name"] for skill in discovery.discover(frame, tmp_path, warnings=warnings)]

    # Standalone skills survive both failed plugin sources, and the catalog says why.
    warning = (
        "Codex plugin skills unavailable: `codex plugin list` exited 1 and "
        "`codex app-server` could not list skills; see the host log for details."
    )
    assert names(warnings := []) == ["later", "local"]
    assert warnings == [warning]

    # Within its short TTL the incomplete catalog is served from the cache, warning included.
    cli.side_effect = None
    cli.return_value = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            {
                "installed": [
                    {
                        "marketplaceName": "market",
                        "name": "toolkit",
                        "version": "1.0.0",
                        "enabled": True,
                    }
                ]
            }
        ),
    )
    assert names(warnings := []) == ["later", "local"]
    assert warnings == [warning]
    assert cli.call_count == 1

    # Once that expires a retry rescans, and the complete catalog is cached for the full TTL.
    now[0] = INCOMPLETE_CATALOG_TTL_SECONDS + 1
    assert names(warnings := []) == ["later", "local", "toolkit:review"]
    assert warnings == []
    cli.side_effect = OSError("gone")
    now[0] += SKILLS_CACHE_TTL_SECONDS - 1
    assert names(warnings := []) == ["later", "local", "toolkit:review"]
    assert warnings == []
    assert cli.call_count == 2


@pytest.mark.posix_only
@pytest.mark.parametrize("blocked", ["launcher-plugin", "launch"])
@pytest.mark.parametrize("catalogs", ["shared", "separate", "session"])
def test_slow_claude_launch_probe_does_not_delay_catalogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogs: str,
    blocked: str,
    _no_configured_claude_launch: Callable[[], bool],
) -> None:
    """Catalogs answer while one probe waits on the launch, then list its plugins once it reports.

    The launcher plugin or the launch blocks until released, so a catalog returned before
    then did not wait for it. Catalog expiry is frozen, so only the finished probe can make
    the cached entry stale.
    """
    for name in ("OMNIGENT_CLAUDE_PATH", "HARNESS_CLAUDE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        plugin_discovery, "claude_launch_is_customized", _no_configured_claude_launch
    )
    resolve = threading.Event()
    if blocked == "launch":
        resolve.set()
    resolved: list[bool] = []

    class GatedLauncher(ClaudeLauncher):
        def launch(self, command: str, args: list[str]) -> tuple[str, list[str]]:
            resolved.append(resolve.wait(10))
            return command, args

    monkeypatch.setenv("OMNIGENT_CLAUDE_LAUNCHER", "gated")
    monkeypatch.setattr("omnigent.claude_launcher._load_launcher", lambda _name: GatedLauncher())
    monkeypatch.setattr("omnigent.host.skills.time", SimpleNamespace(monotonic=lambda: 0.0))
    home = tmp_path / "home"
    for root, name in ((home / ".claude", "local"), (tmp_path / "launch" / "dev", "review")):
        skill = root / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_skill(name))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    release = tmp_path / "release"
    launches = tmp_path / "launches.jsonl"
    plugins = [{"name": "dev", "path": str(tmp_path / "launch" / "dev"), "source": "dev@inline"}]
    init = json.dumps({"type": "system", "subtype": "init", "plugins": plugins})
    configure_claude_launch(
        isolate_provider_resolution(monkeypatch, tmp_path / "config"),
        launches,
        f"while not os.path.exists({str(release)!r}):\n    time.sleep(0.05)\nprint({init!r})\n",
    )
    session_bundle = _bundle({}, executor={"config": {"harness": "claude-native"}})

    def fetch_bundle(_frame: HostSkillsFrame) -> httpx.Response:
        if catalogs != "session":
            pytest.fail("No bundle expected")
        return _response(session_bundle)

    shared = HostSkillDiscovery(fetch_bundle)
    discoveries = [
        HostSkillDiscovery(fetch_bundle) if catalogs == "separate" else shared for _ in range(2)
    ]
    workspaces = [tmp_path / "first", tmp_path / "second"]
    for workspace in workspaces:
        workspace.mkdir()

    def catalog(discovery: HostSkillDiscovery, workspace: Path) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        if catalogs == "session":
            frame = HostSkillsFrame("request", "session", str(workspace), "session", "agent", "1")
        else:
            frame = HostSkillsFrame("request", "claude-native", str(workspace))
        skills = discovery.discover(frame, workspace, warnings=warnings)
        return [skill["name"] for skill in skills], warnings

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            pending = [
                pool.submit(catalog, discovery, workspace)
                for discovery, workspace in zip(discoveries, workspaces, strict=True)
            ]
            # Shorter than a lookup's wait on a launch, so a catalog that waited is still running.
            _, still_running = futures_wait(pending, timeout=5)
        finally:
            resolve.set()
            release.touch()
        loading = [future.result() for future in pending]
    assert not still_running, "a catalog waited on the gated launch"
    plugin_discovery.claude_launch_plugin_dirs()
    # Catalogs built from the stored answer must not run the launcher plugin again.
    resolve.clear()
    loaded = list(map(catalog, discoveries, workspaces))

    note = "Claude plugins added by the configured launch are still loading; retry in a moment."
    assert loading == [(["local"], [note])] * 2
    assert loaded == [(["local", "dev:review"], [])] * 2
    assert resolved == [True]
    assert len(launches.read_text().splitlines()) == 1


@pytest.mark.posix_only
def test_skill_reads_wait_for_a_claude_launch_until_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _no_configured_claude_launch: Callable[[], bool],
) -> None:
    """Session skills and skill content wait for a launch's plugins, but not past its deadline.

    A launcher plugin that hangs is then reported as failed rather than loading forever.
    A skill that is already listed is read without waiting.
    """
    for name in ("OMNIGENT_CLAUDE_PATH", "HARNESS_CLAUDE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        plugin_discovery, "claude_launch_is_customized", _no_configured_claude_launch
    )
    now = [0.0]
    monkeypatch.setattr(plugin_discovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    resolve = threading.Event()
    entered: list[str] = []
    resolved: list[bool] = []

    class GatedLauncher(ClaudeLauncher):
        def launch(self, command: str, args: list[str]) -> tuple[str, list[str]]:
            entered.append(os.environ["OMNIGENT_CLAUDE_LAUNCHER"])
            resolved.append(resolve.wait(10))
            return command, args

    monkeypatch.setenv("OMNIGENT_CLAUDE_LAUNCHER", "gated")
    monkeypatch.setattr("omnigent.claude_launcher._load_launcher", lambda _name: GatedLauncher())
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "local").mkdir(parents=True)
    (home / ".claude" / "skills" / "local" / "SKILL.md").write_text(_skill("local"))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    plugin = tmp_path / "launch" / "dev"
    (plugin / "skills" / "review").mkdir(parents=True)
    (plugin / "skills" / "review" / "SKILL.md").write_text(_skill("review"))
    plugins = [{"name": "dev", "path": str(plugin), "source": "dev@inline"}]
    init = {"type": "system", "subtype": "init", "plugins": plugins}
    launches = tmp_path / "launches.jsonl"
    configure_claude_launch(
        isolate_provider_resolution(monkeypatch, tmp_path / "config"),
        launches,
        f"print({json.dumps(init)!r})\n",
    )
    spec = load(
        _bundle({}, executor={"config": {"harness": "claude-native"}}),
        dest=tmp_path / "bundle",
        expand_env=False,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def session_skills() -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        skills = resolve_session_skills(spec, (workspace,), None, warnings=warnings)
        return [skill.name for skill in skills], warnings

    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = [
            pool.submit(session_skills),
            pool.submit(read_skill_content, "claude-native", "dev:review"),
        ]
        try:
            poll_until(lambda: entered or None, timeout_s=10, interval_s=0.05)
            done, _ = futures_wait(waiting, timeout=0.5)
            local = read_skill_content("claude-native", "local")
            assert resolved == []
        finally:
            resolve.set()
        assert not done
        assert local["name"] == "local"
        assert waiting[0].result(timeout=20) == (["local", "dev:review"], [])
        assert "Private instructions" in waiting[1].result(timeout=20)["content"]

    # A new launch identity starts a probe whose launcher plugin hangs past its deadline.
    resolve.clear()
    monkeypatch.setenv("OMNIGENT_CLAUDE_LAUNCHER", "hung")
    try:
        with pytest.raises(plugin_discovery.LaunchPluginsPending):
            plugin_discovery.claude_launch_plugin_dirs(wait=False)
        poll_until(lambda: len(entered) == 2 or None, timeout_s=10, interval_s=0.05)
        now[0] += plugin_discovery.LAUNCH_PLUGINS_DEADLINE_S
        within = f"did not answer within {plugin_discovery.LAUNCH_PLUGINS_DEADLINE_S:g}s"
        stalled = (
            "Claude plugins added by the configured launch are missing: the launch "
            f"{within}; see the host log for details."
        )
        assert session_skills() == (["local"], [stalled])
        with pytest.raises(LookupError):
            read_skill_content("claude-native", "dev:review")
        assert entered == ["gated", "hung"]
        assert caplog.text.count(within) == 1
        generation = plugin_discovery.launch_plugins_generation()
    finally:
        resolve.set()
    poll_until(
        lambda: plugin_discovery.launch_plugins_generation() > generation,
        timeout_s=10,
        interval_s=0.05,
    )
    assert session_skills() == (["local", "dev:review"], [])
    assert resolved == [True, True]
    assert len(launches.read_text().splitlines()) == 2
