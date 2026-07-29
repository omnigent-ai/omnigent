from __future__ import annotations

import json
from pathlib import Path

from omnigent import install_ledger


def test_install_ledger_round_trip_and_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    profile = tmp_path / ".zshrc"
    profile.write_text(
        "before\n"
        f"{install_ledger.PROFILE_MARKER_BEGIN}\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        f"{install_ledger.PROFILE_MARKER_END}\n"
        "after\n"
    )

    ledger = install_ledger.new_ledger(source="installer", strategy="install", deep=False)
    path = install_ledger.ledger_path()
    install_ledger.write_ledger(ledger, path=path)

    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = install_ledger.load_ledger(path)
    assert loaded is not None
    assert loaded.to_dict() == ledger.to_dict()
    assert loaded.entries.profiles[0].path == str(profile)


def test_backfill_requires_anchor_signal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert install_ledger.backfill_install_ledger(deep=False, apply=True) is None
    assert not install_ledger.backfill_ledger_path().exists()


def test_write_install_ledger_from_env_records_profile_and_dep_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    profile = home / ".profile"
    profile.write_text(
        f"{install_ledger.PROFILE_MARKER_BEGIN}\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        f"{install_ledger.PROFILE_MARKER_END}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state))
    monkeypatch.setenv("OMNIGENT_LEDGER_PROFILE", str(profile))
    monkeypatch.setenv("OMNIGENT_LEDGER_DEP_UV", "omnigent")

    ledger = install_ledger.write_install_ledger_from_env()

    data = json.loads(install_ledger.ledger_path().read_text())
    assert data["ledger_source"] == "installer"
    assert data["installation_id"] == "install-123"
    assert data["entries"]["profiles"][0]["path"] == str(profile)
    assert ledger.entries.deps["uv"].installed_by == "omnigent"
    assert ledger.entries.deps["uv"].confidence == "certain"


def test_installer_ledger_supersedes_backfill_and_preserves_dep_ownership(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state))

    backfill = install_ledger.new_ledger(source="backfill", strategy="deep-backfill", deep=False)
    install_ledger.write_ledger(backfill, path=install_ledger.backfill_ledger_path())
    installer = install_ledger.new_ledger(source="installer", strategy="install", deep=False)
    installer.entries.deps["uv"] = install_ledger.DepEntry(
        present=True,
        path="/tmp/uv",
        version="uv 1.0",
        installed_by="omnigent",
        confidence="certain",
    )
    install_ledger.write_ledger(installer, path=install_ledger.ledger_path())

    rewritten = install_ledger.write_install_ledger_from_env()

    assert rewritten.ledger_source == "installer"
    assert rewritten.entries.deps["uv"].installed_by == "omnigent"
    assert install_ledger.backfill_ledger_path().exists()


def test_backfill_skips_write_when_content_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    state = home / ".omnigent"
    state.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state))

    existing = install_ledger.new_ledger(source="backfill", strategy="fast-backfill", deep=False)
    install_ledger.write_ledger(existing, path=install_ledger.backfill_ledger_path())

    def fail_write(*args, **kwargs) -> None:
        raise AssertionError("unchanged backfill should not be rewritten")

    monkeypatch.setattr(install_ledger, "write_ledger", fail_write)

    resolved = install_ledger.backfill_install_ledger(deep=False, apply=True)

    assert resolved is not None
    assert resolved.to_dict() == existing.to_dict()


def test_install_ledger_merges_existing_and_observed_external_configs(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state = home / ".omnigent"
    cursor_dir = workspace / ".cursor"
    state.mkdir(parents=True)
    cursor_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state))
    monkeypatch.chdir(workspace)

    existing = install_ledger.new_ledger(source="installer", strategy="install", deep=False)
    old_config = install_ledger.ExternalConfigEntry(
        path=str(tmp_path / "old.json"),
        marker="mcpServers.omnigent",
        format="json",
        allowlist=["mcpServers.omnigent"],
    )
    existing.entries.injected_external_config = [old_config]
    install_ledger.write_ledger(existing, path=install_ledger.ledger_path())
    observed_config = cursor_dir / "mcp.json"
    observed_config.write_text('{"mcpServers": {"omnigent": {"command": "python"}}}\n')

    rewritten = install_ledger.write_install_ledger_from_env()

    config_paths = {entry.path for entry in rewritten.entries.injected_external_config}
    assert str(tmp_path / "old.json") in config_paths
    assert str(observed_config) in config_paths


def test_deep_backfill_observes_external_config_and_launch_agents(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state = home / ".omnigent"
    cursor_dir = workspace / ".cursor"
    launch_dir = home / "Library" / "LaunchAgents"
    state.mkdir(parents=True)
    cursor_dir.mkdir(parents=True)
    launch_dir.mkdir(parents=True)
    (state / "installation_id").write_text("install-123\n")
    cursor_config = cursor_dir / "mcp.json"
    cursor_config.write_text('{"mcpServers": {"omnigent": {"command": "python"}}}\n')
    launch_agent = launch_dir / "ai.omnigent.local.plist"
    launch_agent.write_text("plist\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(state))
    monkeypatch.chdir(workspace)

    ledger = install_ledger.backfill_install_ledger(deep=True, apply=False)

    assert ledger is not None
    assert ledger.entries.injected_external_config[0].path == str(cursor_config)
    assert ledger.entries.injected_external_config[0].marker == "mcpServers.omnigent"
    assert ledger.entries.launch_agents[0].path == str(launch_agent)
    assert cursor_config.exists()
    assert launch_agent.exists()
