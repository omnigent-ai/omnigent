"""Codex remote-resume compatibility for CLI spellings and user profiles."""

import ntpath
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tomlkit

from omnigent.harnesses.codex_native import app_server, launch_args
from omnigent.harnesses.codex_native.launch_args import (
    absolute_codex_path,
    canonical_codex_launch_args,
    codex_config_profile,
    materialize_codex_config_profile,
)


def test_codex_paths_expand_home_without_resolving_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert absolute_codex_path("link/file", tmp_path) == str(link / "file")
    assert absolute_codex_path("~/link/file", tmp_path / "other") == str(link / "file")


def test_codex_paths_expand_windows_home_with_either_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USERPROFILE", r"C:\Users\test")
    monkeypatch.setattr(launch_args, "os", SimpleNamespace(path=ntpath, sep="\\"))
    for path in ("~/rules.md", "~\\rules.md"):
        assert absolute_codex_path(path, Path("D:/codex")) == r"C:\Users\test\rules.md"
    assert absolute_codex_path("~other/rules.md", Path("D:/codex")) == r"D:\codex\~other\rules.md"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--yolo",), {"approvalPolicy": "never", "sandbox": "danger-full-access"}),
        (
            ("--approve-for-me",),
            {
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
                "approvalsReviewer": "auto_review",
            },
        ),
        (
            ("--not-so-yolo",),
            {
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
                "approvalsReviewer": "auto_review",
            },
        ),
        (("-sread-only", "-anever"), {"sandbox": "read-only", "approvalPolicy": "never"}),
        (("-s=read-only", "-a=never"), {"sandbox": "read-only", "approvalPolicy": "never"}),
        (
            ("-csandbox_workspace_write.network_access=false", "-anever"),
            {
                "approvalPolicy": "never",
                "config": {"sandbox_workspace_write.network_access": False},
            },
        ),
        (("-auntrusted", "-capproval_policy=never"), {"approvalPolicy": "untrusted"}),
        (
            ("--not-so-yolo", "-capprovals_reviewer=user", "-anever"),
            {
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
                "sandbox": "workspace-write",
            },
        ),
    ],
)
def test_remote_resume_option_spellings(args: tuple[str, ...], expected: dict) -> None:
    assert app_server._codex_resume_permission_params(args) == expected
    assert app_server.build_codex_remote_args(
        codex_args=args, thread_id="thread-test", remote_url="ws://127.0.0.1:9876"
    ) == ["resume", "--remote", "ws://127.0.0.1:9876", "thread-test"]


@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "strict"),
        ("--profile=strict",),
        ("-p", "strict"),
        ("-p=strict",),
        ("-pstrict",),
    ],
)
def test_profile_selector_applied_by_server_not_terminal(args: tuple[str, ...]) -> None:
    assert codex_config_profile(args) == "strict"
    assert app_server._codex_resume_permission_params(args) == {}
    for thread_id in (None, "thread-test"):
        result = app_server.build_codex_remote_args(
            codex_args=args, thread_id=thread_id, remote_url="ws://127.0.0.1:9876"
        )
        assert "strict" not in result
        assert "-c" not in result


def test_options_do_not_parse_values_or_prompt() -> None:
    args = ("-c", 'developer_instructions="--yolo -sread-only"', "--", "-pstrict", "--yolo")
    assert canonical_codex_launch_args(args) == list(args)
    assert codex_config_profile(args) is None


@pytest.mark.parametrize("profile", ["../other", "/tmp/other", "", "two/parts"])
def test_invalid_profile_names(profile: str) -> None:
    with pytest.raises(ValueError):
        codex_config_profile(("--profile", profile))


@pytest.mark.parametrize("version", [None, (0, 134, 0), (0, 154, 0), (0, 155, 0)])
def test_profile_materialization_preserves_layers_and_private_edits(
    tmp_path: Path, version: tuple[int, int, int] | None
) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    original = (
        'approval_policy="untrusted"\nmodel="base-model"\n'
        '[sandbox_workspace_write]\nnetwork_access=false\nwritable_roots=["/base"]\n'
    )
    (source / "config.toml").write_text(original)
    (private / "config.toml").write_text(original)
    (source / "strict.config.toml").write_text(
        'sandbox_mode="read-only"\nmodel="profile-model"\n[sandbox_workspace_write]\nwritable_roots=["/profile"]\n'
    )
    materialize_codex_config_profile(private, source, "strict", codex_version=version)
    config = tomlkit.parse((private / "config.toml").read_text())
    assert config["approval_policy"] == "untrusted"
    assert config["sandbox_mode"] == "read-only"
    assert config["model"] == "profile-model"
    assert config["sandbox_workspace_write"] == {
        "network_access": False,
        "writable_roots": ["/profile"],
    }
    config["model"] = "edited-model"
    (private / "config.toml").write_text(tomlkit.dumps(config))
    materialize_codex_config_profile(private, source, None, codex_version=version)
    restored = tomlkit.parse((private / "config.toml").read_text())
    assert "sandbox_mode" not in restored
    assert restored["model"] == "edited-model"
    assert restored["sandbox_workspace_write"]["writable_roots"] == ["/base"]
    assert (source / "config.toml").read_text() == original


def test_missing_profile_does_not_mutate_private_config(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    original = 'sandbox_mode="read-only"\n'
    (private / "config.toml").write_text(original)
    with pytest.raises(FileNotFoundError):
        materialize_codex_config_profile(private, source, "missing", codex_version=(0, 155, 0))
    assert (private / "config.toml").read_text() == original


@pytest.mark.parametrize("operation", ["apply", "switch", "remove"])
@pytest.mark.parametrize("failed_replace", [1, 2, 3])
def test_profile_update_recovers_after_interrupted_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    failed_replace: int,
) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    config_path = private / "config.toml"
    config_path.write_text('model="base"\nsandbox_mode="read-only"\n')
    (source / "first.config.toml").write_text('model="first"\nsandbox_mode="workspace-write"\n')
    (source / "second.config.toml").write_text('model="second"\napproval_policy="never"\n')
    if operation != "apply":
        materialize_codex_config_profile(private, source, "first", codex_version=(0, 155, 0))
        config = tomlkit.parse(config_path.read_text())
        config["model"] = "edited"
        config_path.write_text(tomlkit.dumps(config))
    selected = {"apply": "first", "switch": "second", "remove": None}[operation]
    replace = launch_args.os.replace
    attempts = 0

    def fail_replace(source_path: str, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == failed_replace:
            raise OSError("injected write failure")
        replace(source_path, destination)

    with monkeypatch.context() as fault:
        fault.setattr(launch_args.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected write failure"):
            materialize_codex_config_profile(private, source, selected, codex_version=(0, 155, 0))
    assert sorted(path.name for path in private.iterdir()) in (
        ["config.toml"],
        [".omnigent-config-profile.toml", "config.toml"],
    )
    materialize_codex_config_profile(private, source, selected, codex_version=(0, 155, 0))
    effective = tomlkit.parse(config_path.read_text()).unwrap()
    assert (
        effective["model"] == {"apply": "first", "switch": "second", "remove": "edited"}[operation]
    )
    assert effective["sandbox_mode"] == (
        "workspace-write" if operation == "apply" else "read-only"
    )
    materialize_codex_config_profile(private, source, None, codex_version=(0, 155, 0))
    assert tomlkit.parse(config_path.read_text()).unwrap() == {
        "model": "base" if operation == "apply" else "edited",
        "sandbox_mode": "read-only",
    }
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert (private / ".omnigent-config-profile.toml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "state",
    ["invalid=[", "", "base=1\n[applied]\n", "[base]\n", "pending=1\n[base]\n[applied]\n"],
)
def test_malformed_profile_state_preserves_both_files(tmp_path: Path, state: str) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    config_path = private / "config.toml"
    config_path.write_text('model="base"\n')
    state_path = private / ".omnigent-config-profile.toml"
    state_path.write_text(state)
    with pytest.raises(ValueError, match="Invalid Codex profile state"):
        materialize_codex_config_profile(private, source, None, codex_version=(0, 155, 0))
    assert config_path.read_text() == 'model="base"\n'
    assert state_path.read_text() == state


def test_pending_profile_state_rejects_unexpected_config_without_writing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    config_path = private / "config.toml"
    original = 'model="concurrent-edit"\n'
    config_path.write_text(original)
    state_path = private / ".omnigent-config-profile.toml"
    state = tomlkit.dumps(
        {"base": {"model": "base"}, "applied": {"model": "base"}, "pending": {"model": "profile"}}
    )
    state_path.write_text(state)
    with pytest.raises(ValueError, match="incomplete profile update"):
        materialize_codex_config_profile(private, source, None, codex_version=(0, 155, 0))
    assert config_path.read_text() == original
    assert state_path.read_text() == state


def test_profile_replaces_config_symlink_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    original = 'model="base"\n'
    (source / "config.toml").write_text(original)
    (source / "strict.config.toml").write_text('model="profile"\n')
    (private / "config.toml").symlink_to(source / "config.toml")
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 155, 0))
    assert not (private / "config.toml").is_symlink()
    assert (source / "config.toml").read_text() == original


def test_profile_reapplication_retains_private_edits_for_removal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    config_path = private / "config.toml"
    config_path.write_text('model="base"\n')
    (source / "strict.config.toml").write_text('model="profile"\n')
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 155, 0))
    config_path.write_text('model="edited"\n')
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 155, 0))
    assert tomlkit.parse(config_path.read_text())["model"] == "profile"
    materialize_codex_config_profile(private, source, None, codex_version=(0, 155, 0))
    assert tomlkit.parse(config_path.read_text())["model"] == "edited"


def test_profile_paths_keep_source_origin_and_symbolic_permission_keys(tmp_path: Path) -> None:
    source = tmp_path / "source"
    private = tmp_path / "private"
    source.mkdir()
    private.mkdir()
    profile = {
        "model_instructions_file": "instructions.md",
        "model_catalog_json": "catalog.json",
        "sandbox_workspace_write": {"writable_roots": ["output"]},
        "agents": {"reviewer": {"config_file": "agents/reviewer.toml"}},
        "skills": {"config": [{"path": "skills/review", "enabled": True}]},
        "model_providers": {"fixture": {"auth": {"cwd": "auth"}}},
        "otel": {"exporter": {"otlp-http": {"tls": {"ca-certificate": "ca.pem"}}}},
        "permissions": {"strict": {"filesystem": {":workspace_roots": {"src/**": "read"}}}},
        "mcp_servers": {"fixture": {"cwd": "relative-tool-cwd", "command": "tool"}},
        "developer_instructions": "instructions.md",
    }
    original = tomlkit.dumps(profile)
    (source / "strict.config.toml").write_text(original)
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 155, 0))
    config = tomlkit.parse((private / "config.toml").read_text()).unwrap()
    assert config["model_instructions_file"] == str(source / "instructions.md")
    assert config["model_catalog_json"] == str(source / "catalog.json")
    assert config["sandbox_workspace_write"]["writable_roots"] == [str(source / "output")]
    assert config["agents"]["reviewer"]["config_file"] == str(source / "agents/reviewer.toml")
    assert config["skills"]["config"][0]["path"] == str(source / "skills/review")
    assert config["model_providers"]["fixture"]["auth"]["cwd"] == str(source / "auth")
    assert config["otel"]["exporter"]["otlp-http"]["tls"]["ca-certificate"] == str(
        source / "ca.pem"
    )
    assert config["permissions"] == profile["permissions"]
    assert config["mcp_servers"] == profile["mcp_servers"]
    assert config["developer_instructions"] == profile["developer_instructions"]
    assert (source / "strict.config.toml").read_text() == original


async def test_remote_resume_add_dir_preserves_configured_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = AsyncMock()
    client.request.return_value = {
        "result": {"config": {"sandbox_workspace_write": {"writable_roots": ["/configured"]}}}
    }
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    args = (
        "--add-dir=extra",
        "--add-dir",
        "/second",
        "-sworkspace-write",
        "-c",
        "sandbox_workspace_write.network_access=false",
    )
    await app_server.preload_codex_thread_for_resume(
        "ws://127.0.0.1:9876", "thread-test", terminal_launch_args=args, cwd=tmp_path
    )
    resume = client.request.call_args_list[-1].args[1]
    assert resume["runtimeWorkspaceRoots"] == [
        str(tmp_path),
        str(tmp_path / "extra"),
        "/second",
        "/configured",
    ]
    assert resume["config"]["sandbox_workspace_write.network_access"] is False
    assert app_server.build_codex_remote_args(
        codex_args=args, thread_id="thread-test", remote_url="ws://127.0.0.1:9876"
    ) == ["resume", "--remote", "ws://127.0.0.1:9876", "thread-test"]


def test_profile_legacy_selection_overrides_base_named_selection(tmp_path: Path) -> None:
    private = tmp_path / "private"
    source = tmp_path / "source"
    private.mkdir()
    source.mkdir()
    (private / "config.toml").write_text('default_permissions=":danger-full-access"\n')
    (source / "strict.config.toml").write_text('sandbox_mode="read-only"\n')
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 155, 0))
    config = tomlkit.parse((private / "config.toml").read_text())
    assert config["sandbox_mode"] == "read-only"
    assert "default_permissions" not in config
    materialize_codex_config_profile(private, source, None, codex_version=(0, 155, 0))
    assert (
        tomlkit.parse((private / "config.toml").read_text())["default_permissions"]
        == ":danger-full-access"
    )


def test_legacy_codex_profile_materialization(tmp_path: Path) -> None:
    private = tmp_path / "private"
    source = tmp_path / "source"
    private.mkdir()
    source.mkdir()
    (private / "config.toml").write_text('[profiles.strict]\nsandbox_mode="read-only"\n')
    materialize_codex_config_profile(private, source, "strict", codex_version=(0, 133, 0))
    assert tomlkit.parse((private / "config.toml").read_text())["sandbox_mode"] == "read-only"


async def test_remote_resume_named_permissions_add_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = AsyncMock()
    client.request.return_value = {
        "result": {
            "config": {
                "default_permissions": "restricted",
                "sandbox_workspace_write": {"writable_roots": ["/inactive-legacy-root"]},
            }
        }
    }
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    await app_server.preload_codex_thread_for_resume(
        "ws://127.0.0.1:9876",
        "thread-test",
        cwd=tmp_path,
        terminal_launch_args=("--add-dir=extra", "-cdefault_permissions=restricted"),
    )
    params = client.request.call_args_list[-1].args[1]
    assert params["permissions"] == "restricted"
    assert params["runtimeWorkspaceRoots"] == [str(tmp_path), str(tmp_path / "extra")]


async def test_remote_resume_config_read_failure_does_not_discard_add_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = AsyncMock()
    client.request.side_effect = RuntimeError("config unavailable")
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match="config unavailable"):
        await app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:9876",
            "thread-test",
            cwd=tmp_path,
            terminal_launch_args=("--add-dir=extra",),
        )
    client.close.assert_awaited_once()
    assert client.request.call_count == 1


async def test_remote_resume_relative_config_roots_are_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = AsyncMock()
    client.request.return_value = {"result": {"config": {}}}
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    await app_server.preload_codex_thread_for_resume(
        "ws://127.0.0.1:9876",
        "thread-test",
        cwd=tmp_path,
        terminal_launch_args=(
            "--add-dir=extra",
            "-sworkspace-write",
            '-csandbox_workspace_write.writable_roots=["relative-output"]',
        ),
    )
    params = client.request.call_args_list[-1].args[1]
    assert params["runtimeWorkspaceRoots"] == [
        str(tmp_path),
        str(tmp_path / "extra"),
        str(tmp_path / "relative-output"),
    ]
