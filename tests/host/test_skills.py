"""Host catalogs preserve session scope without depending on a runner."""

from __future__ import annotations

import io
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

from omnigent.host.frames import HostSkillsFrame
from omnigent.host.skills import HostSkillDiscovery
from omnigent.spec import load
from omnigent.spec.skill_sources import resolve_session_skills


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
