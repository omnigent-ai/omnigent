"""In-process launch planning; the real CLI is only a config-drift test oracle."""

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.mcp_disabled_servers import ServerKey, plan_codex_launch, set_disabled


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "omnigent"))
    home = tmp_path / "private-home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    return home, repo


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _plan(home, cwd, overrides=(), *, active=("codex",), executor="codex"):
    return plan_codex_launch(
        overrides, executor=executor, active_harnesses=active, codex_home=home, cwd=cwd
    )


@pytest.mark.parametrize("executor", ["codex", "codex-native"])
@pytest.mark.parametrize("active", [None, [], ["claude"], ["codex"]])
def test_no_entries_does_not_read_config(launch, monkeypatch, executor, active):
    home, repo = launch
    with patch.object(Path, "read_text", side_effect=AssertionError("must not read")):
        plan = _plan(home, repo, ["base=true"], active=active, executor=executor)
    assert plan.config_overrides == plan.fallback_config_overrides == ("base=true",)
    assert plan.outcome == "none"
    assert plan.overrides_after_early_exit() is None


@pytest.mark.parametrize("active", [None, [], ["claude"]])
def test_inactive_ignores_saved_entries(launch, active):
    home, repo = launch
    set_disabled(ServerKey("codex", "user"), True)
    _write(home / "config.toml", '[mcp_servers.user]\ncommand="true"\n')
    assert _plan(home, repo, ["base=true"], active=active).config_overrides == ("base=true",)


@pytest.mark.parametrize("executor", ["codex", "codex-native"])
def test_user_config_names_only_and_dropped_logged_once(launch, executor, caplog):
    home, repo = launch
    for name in ["github", "missing", "already_off"]:
        set_disabled(ServerKey("codex", name), True)
    _write(
        home / "config.toml",
        '[mcp_servers.github]\ncommand="true"\n'
        '[mcp_servers.already_off]\ncommand="true"\nenabled=false\n',
    )
    plan = _plan(home, repo, ["base=true"], executor=executor)
    assert plan.config_overrides == (
        "base=true",
        "mcp_servers.github.enabled=false",
        "mcp_servers.already_off.enabled=false",
    )
    assert plan.outcome == "applied"
    assert plan.dropped == ("missing",)
    assert plan.overrides_after_early_exit() == ("base=true",)
    assert [r.getMessage() for r in caplog.records if "not_reported" in r.getMessage()] == [
        "mcp_disabled_servers_not_reported names=('missing',)"
    ]


def test_prepared_profile_mcp_config(launch):
    from omnigent.harnesses.codex_native.launch_args import (
        materialize_codex_config_profile,
        read_codex_mcp_servers,
    )

    home, repo = launch
    source = home.parent / "source"
    _write(source / "config.toml", '[mcp_servers.user]\ncommand="true"\n')
    _write(source / "work.config.toml", '[mcp_servers.profile]\ncommand="true"\n')
    shutil.copyfile(source / "config.toml", home / "config.toml")
    materialize_codex_config_profile(home, source, "work", codex_version=(0, 163, 0))
    for name in read_codex_mcp_servers(source, "work", codex_version=(0, 163, 0)):
        set_disabled(ServerKey("codex", name), True)
    assert _plan(home, repo).config_overrides == (
        "mcp_servers.user.enabled=false",
        "mcp_servers.profile.enabled=false",
    )


@pytest.mark.parametrize("trust", ["trusted", "untrusted", None])
@pytest.mark.parametrize("defined", [True, False])
def test_repo_layer_requires_trust_and_definition(launch, trust, defined):
    home, repo = launch
    set_disabled(ServerKey("codex", "repo"), True)
    if trust:
        _write(
            home / "config.toml", f'[projects.{json.dumps(str(repo))}]\ntrust_level="{trust}"\n'
        )
    _write(
        repo / ".codex/config.toml",
        '[mcp_servers.repo]\nurl="http://localhost/mcp"\n' if defined else "",
    )
    plan = _plan(home, repo)
    assert plan.config_overrides == (
        ("mcp_servers.repo.enabled=false",) if defined and trust == "trusted" else ()
    )


@pytest.mark.parametrize("markers", [None, '["ROOT"]', "[]"])
def test_root_marker_walk_and_layer_order(launch, markers):
    home, repo = launch
    cwd = repo / "nested/work"
    cwd.mkdir(parents=True)
    (repo / "nested/ROOT").touch()
    config = f'[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n'
    if markers is not None:
        config = f"project_root_markers={markers}\n" + config
    _write(home / "config.toml", config)
    for folder, name in [
        (repo.parent, "outside"),
        (repo, "root"),
        (repo / "nested", "middle"),
        (cwd, "leaf"),
    ]:
        set_disabled(ServerKey("codex", name), True)
        _write(folder / ".codex/config.toml", f'[mcp_servers.{name}]\ncommand="true"\n')
    expected = {
        None: (
            "mcp_servers.root.enabled=false",
            "mcp_servers.middle.enabled=false",
            "mcp_servers.leaf.enabled=false",
        ),
        '["ROOT"]': ("mcp_servers.middle.enabled=false", "mcp_servers.leaf.enabled=false"),
        "[]": ("mcp_servers.leaf.enabled=false",),
    }
    assert _plan(home, cwd).config_overrides == expected[markers]


def test_per_layer_trust_cwd_does_not_trust_parent(launch):
    home, repo = launch
    cwd = repo / "nested"
    cwd.mkdir()
    _write(home / "config.toml", f'[projects.{json.dumps(str(cwd))}]\ntrust_level="trusted"\n')
    for folder, name in [(repo, "parent"), (cwd, "child")]:
        set_disabled(ServerKey("codex", name), True)
        _write(folder / ".codex/config.toml", f'[mcp_servers.{name}]\ncommand="true"\n')
    assert _plan(home, cwd).config_overrides == ("mcp_servers.child.enabled=false",)


def test_untrusted_child_overrides_trusted_root(launch):
    home, repo = launch
    cwd = repo / "nested"
    cwd.mkdir()
    _write(
        home / "config.toml",
        f'[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n[projects.{json.dumps(str(cwd))}]\ntrust_level="untrusted"\n',
    )
    for folder, name in [(repo, "parent"), (cwd, "child")]:
        set_disabled(ServerKey("codex", name), True)
        _write(folder / ".codex/config.toml", f'[mcp_servers.{name}]\ncommand="true"\n')
    assert _plan(home, cwd).config_overrides == ("mcp_servers.parent.enabled=false",)


@pytest.mark.parametrize("content", ["[broken", b"\xff", None])
def test_unreadable_layer_skipped_and_logged_once(launch, caplog, content):
    home, repo = launch
    set_disabled(ServerKey("codex", "repo"), True)
    _write(home / "config.toml", f'[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n')
    path = repo / ".codex/config.toml"
    path.parent.mkdir()
    if content is None:
        path.mkdir()  # Read failure, even when tests run as root.
    else:
        path.write_bytes(content.encode() if isinstance(content, str) else content)
    for _ in range(2):
        plan = _plan(home, repo)
        assert plan.config_overrides == ()
        assert plan.dropped == ("repo",)
    assert (
        len(
            [
                r
                for r in caplog.records
                if "mcp_disabled_codex_config_read_failed" in r.getMessage()
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (['mcp_servers.cli.command="true"'], ("cli",)),
        (['mcp_servers={cli={command="true"}}'], ("cli",)),
        (["mcp_servers=false"], ()),
        (['mcp_servers={first={command="true"}}', 'mcp_servers={cli={command="true"}}'], ("cli",)),
        (["mcp_servers.cli.command=true", "mcp_servers.cli.command=echo"], ("cli",)),
    ],
)
def test_caller_overrides(launch, overrides, expected):
    home, repo = launch
    for name in ["cli", "first"]:
        set_disabled(ServerKey("codex", name), True)
    plan = _plan(home, repo, overrides)
    assert plan.config_overrides == (
        *overrides,
        *(f"mcp_servers.{name}.enabled=false" for name in expected),
    )
    assert plan.fallback_config_overrides == tuple(overrides)


def test_cli_trust_and_root_markers_precede_discovery(launch):
    home, repo = launch
    cwd = repo / "nested"
    cwd.mkdir()
    (repo / "ROOT").touch()
    _write(repo / ".codex/config.toml", '[mcp_servers.repo]\ncommand="true"\n')
    set_disabled(ServerKey("codex", "repo"), True)
    overrides = [
        'project_root_markers=["ROOT"]',
        f'projects={{{json.dumps(str(repo))}={{trust_level="trusted"}}}}',
    ]
    assert _plan(home, cwd, overrides).config_overrides[-1] == "mcp_servers.repo.enabled=false"


@pytest.mark.parametrize("name", ['name.with."quotes', "server😀", "server with spaces"])
def test_quoted_name(launch, name):
    home, repo = launch
    set_disabled(ServerKey("codex", name), True)
    _write(
        home / "config.toml",
        f'[mcp_servers.{json.dumps(name, ensure_ascii=False)}]\ncommand="true"\n',
    )
    overrides = ['mcp_servers={superseded={command="true"}}', 'mcp_servers.cli.command="true"']
    plan = _plan(home, repo, overrides)
    assert plan.fallback_config_overrides == tuple(overrides)
    assert plan.config_overrides[:-1] == tuple(overrides)
    assert tomllib.loads(plan.config_overrides[-1]) == {
        "mcp_servers": {
            "superseded": {"command": "true"},
            "cli": {"command": "true"},
            name: {"enabled": False},
        }
    }


@pytest.mark.parametrize("valid_registration", [True, False])
def test_linked_worktree_uses_main_checkout_trust(launch, valid_registration):
    home, main = launch
    worktree = main.parent / "worktree"
    worktree.mkdir()
    metadata = main / ".git/worktrees/session"
    _write(metadata / "commondir", "../..\n")
    _write(metadata / "gitdir", str(worktree / (".git" if valid_registration else "other")))
    _write(worktree / ".git", f"gitdir: {metadata}\n")
    _write(home / "config.toml", f'[projects.{json.dumps(str(main))}]\ntrust_level="trusted"\n')
    _write(worktree / ".codex/config.toml", '[mcp_servers.worktree]\ncommand="true"\n')
    set_disabled(ServerKey("codex", "worktree"), True)
    assert _plan(home, worktree).config_overrides == (
        ("mcp_servers.worktree.enabled=false",) if valid_registration else ()
    )


@pytest.mark.parametrize("canonical_trust", ["trusted", "untrusted", None])
def test_symlink_trust_prefers_canonical_then_original_key(launch, canonical_trust):
    home, repo = launch
    alias = repo.parent / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    config = f'[projects.{json.dumps(str(alias))}]\ntrust_level="trusted"\n'
    if canonical_trust:
        config += f'[projects.{json.dumps(str(repo))}]\ntrust_level="{canonical_trust}"\n'
    _write(home / "config.toml", config)
    _write(repo / ".codex/config.toml", '[mcp_servers.repo]\ncommand="true"\n')
    set_disabled(ServerKey("codex", "repo"), True)
    assert _plan(home, alias).config_overrides == (
        () if canonical_trust == "untrusted" else ("mcp_servers.repo.enabled=false",)
    )


def test_malformed_user_layer_does_not_hide_valid_project_layer(launch, caplog):
    home, repo = launch
    _write(home / "config.toml", "[broken")
    _write(repo / ".codex/config.toml", '[mcp_servers.repo]\ncommand="true"\n')
    set_disabled(ServerKey("codex", "repo"), True)
    overrides = [f'projects={{{json.dumps(str(repo))}={{trust_level="trusted"}}}}']
    assert _plan(home, repo, overrides).config_overrides[-1] == "mcp_servers.repo.enabled=false"
    assert "mcp_disabled_codex_config_read_failed" in caplog.text


def test_project_layer_cannot_grant_itself_trust(launch):
    home, repo = launch
    _write(
        repo / ".codex/config.toml",
        f'[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n[mcp_servers.repo]\ncommand="true"\n',
    )
    set_disabled(ServerKey("codex", "repo"), True)
    assert _plan(home, repo).dropped == ("repo",)


@pytest.mark.parametrize("overrides", [["missing-assignment-secret"], ["project_root_markers=42"]])
def test_invalid_discovery_settings_never_raise_or_log_values(launch, caplog, overrides):
    home, repo = launch
    set_disabled(ServerKey("codex", "user"), True)
    _write(home / "config.toml", '[mcp_servers.user]\ncommand="true"\n')
    assert _plan(home, repo, overrides).config_overrides[-1] == "mcp_servers.user.enabled=false"
    assert "mcp_disabled_codex_config_read_failed" in caplog.text
    assert "secret" not in caplog.text


def test_plan_is_synchronous_and_never_spawns(launch):
    home, repo = launch
    set_disabled(ServerKey("codex", "user"), True)
    _write(home / "config.toml", '[mcp_servers.user]\ncommand="true"\n')
    assert not inspect.iscoroutinefunction(plan_codex_launch)
    with (
        patch("subprocess.Popen", side_effect=AssertionError("must not spawn")),
        patch.object(
            asyncio.BaseEventLoop, "subprocess_exec", side_effect=AssertionError("must not spawn")
        ),
        patch.object(
            asyncio.BaseEventLoop, "subprocess_shell", side_effect=AssertionError("must not spawn")
        ),
    ):
        assert _plan(home, repo).outcome == "applied"


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI not installed")
def test_real_codex_config_names_match_in_process_plan(launch):
    home, repo = launch
    shutil.rmtree(repo / ".git")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _write(
        home / "config.toml",
        f'[mcp_servers.user]\ncommand="true"\n[projects.{json.dumps(str(repo))}]\ntrust_level="trusted"\n',
    )
    _write(repo / ".codex/config.toml", '[mcp_servers.repo]\nurl="http://localhost:1/mcp"\n')
    quoted_names = ["quoted.name", "server😀", 'with "quotes"']
    for name in quoted_names:
        with (home / "config.toml").open("a") as stream:
            stream.write(f'[mcp_servers.{json.dumps(name, ensure_ascii=False)}]\ncommand="true"\n')
    overrides = [
        'mcp_servers={superseded={command="true"}}',
        'mcp_servers={cli={command="true"},keep={command="true"},'
        '"cli.dotted"={url="http://localhost:2/mcp"}}',
        'mcp_servers.cli.args=["--flag"]',
    ]
    for name in ["user", "repo", "cli", "cli.dotted", "missing", *quoted_names]:
        set_disabled(ServerKey("codex", name), True)
    plan = _plan(home, repo, overrides)
    argv = ["codex"]
    for override in overrides:
        argv.extend(["-c", override])
    result = subprocess.run(
        [*argv, "mcp", "list", "--json"],
        env={**os.environ, "CODEX_HOME": str(home)},
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    cli_names = {row["name"] for row in json.loads(result.stdout)}
    expected_names = {"user", "repo", "cli", "cli.dotted", "keep", *quoted_names}
    assert cli_names == expected_names
    assert plan.dropped == ("missing",)
    argv = ["codex"]
    for override in plan.config_overrides:
        argv.extend(["-c", override])
    disabled = subprocess.run(
        [*argv, "mcp", "list", "--json"],
        env={**os.environ, "CODEX_HOME": str(home)},
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    rows = json.loads(disabled.stdout)
    assert {row["name"] for row in rows} == expected_names
    assert {row["name"]: row["enabled"] for row in rows} == {
        name: name == "keep" for name in expected_names
    }
    assert {row["name"]: row["transport"] for row in rows} == {
        row["name"]: row["transport"] for row in json.loads(result.stdout)
    }
