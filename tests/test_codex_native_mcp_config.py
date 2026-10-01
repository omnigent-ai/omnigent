"""Tests for syncing the user's Codex MCP servers into a session's private config."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import tomllib

from omnigent.harnesses.codex_native.app_server import _inject_mcp_server_config
from omnigent.harnesses.codex_native.mcp_config import (
    MCP_SOURCE_STATE_FILENAME,
    missing_codex_home_mcp_servers,
    sync_codex_home_mcp_servers,
)

_GITHUB = '[mcp_servers.github]\ncommand = "gh-mcp"\nargs = ["serve"]\n'
_DOCS = '[mcp_servers.docs]\ncommand = "docs-mcp"\n'
_SLACK = '[mcp_servers.slack]\ncommand = "slack-mcp"\n'
_PRIVATE_BASE = (
    'model = "gpt-test"\n\n[projects."/w"]\ntrust_level = "trusted"\n\n'
    '[mcp_servers.omnigent]\ncommand = "/py"\nargs = ["relay"]\n'
)


@pytest.fixture
def homes(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(codex_home, source_home)`` with both directories created."""
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    return codex_home, source_home


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _servers(codex_home: Path) -> dict:
    return _load(codex_home / "config.toml").get("mcp_servers", {})


def test_legacy_home_without_sidecar_gains_missing_servers(homes: tuple[Path, Path]) -> None:
    """A stale copy (no sidecar) gets source servers and keeps everything else."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) == frozenset({"github", "docs"})

    config = _load(codex_home / "config.toml")
    assert config["model"] == "gpt-test"
    assert config["projects"]["/w"]["trust_level"] == "trusted"
    assert config["mcp_servers"]["omnigent"] == {"command": "/py", "args": ["relay"]}
    assert config["mcp_servers"]["github"] == {"command": "gh-mcp", "args": ["serve"]}
    assert config["mcp_servers"]["docs"] == {"command": "docs-mcp"}
    recorded = _load(codex_home / MCP_SOURCE_STATE_FILENAME)["mcp_servers"]
    assert set(recorded) == {"github", "docs"}


def test_legacy_home_never_deletes_private_only_servers(homes: tuple[Path, Path]) -> None:
    """Without a sidecar nothing is known to be removed, so private servers stay."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE + _SLACK, encoding="utf-8")

    sync_codex_home_mcp_servers(codex_home, source_home)

    assert set(_servers(codex_home)) == {"omnigent", "slack", "github"}


def test_source_edit_updates_private_server(homes: tuple[Path, Path]) -> None:
    """A server changed in the source replaces the private table."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    (source_home / "config.toml").write_text(
        '[mcp_servers.github]\ncommand = "gh-mcp"\nargs = ["serve", "--v2"]\n', encoding="utf-8"
    )
    sync_codex_home_mcp_servers(codex_home, source_home)

    assert _servers(codex_home)["github"]["args"] == ["serve", "--v2"]
    assert _load(codex_home / MCP_SOURCE_STATE_FILENAME)["mcp_servers"]["github"]["args"] == [
        "serve",
        "--v2",
    ]


def test_source_removal_deletes_private_server(homes: tuple[Path, Path]) -> None:
    """A server removed from the source since the last sync is removed privately."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    assert set(_servers(codex_home)) == {"omnigent", "github"}
    assert set(_load(codex_home / MCP_SOURCE_STATE_FILENAME)["mcp_servers"]) == {"github"}


def test_removing_last_source_server_drops_only_recorded_servers(
    homes: tuple[Path, Path],
) -> None:
    """Removal leaves the omnigent table and unrelated keys in place."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    (source_home / "config.toml").write_text("", encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    config = _load(codex_home / "config.toml")
    assert set(config["mcp_servers"]) == {"omnigent"}
    assert config["model"] == "gpt-test"


def test_private_only_server_survives_sync(homes: tuple[Path, Path]) -> None:
    """A server added only in the session is never in the source record, so it stays."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)
    with (codex_home / "config.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n" + _SLACK)

    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)

    assert set(_servers(codex_home)) == {"omnigent", "github", "slack", "docs"}


def test_session_local_edit_survives_when_source_unchanged(homes: tuple[Path, Path]) -> None:
    """Editing a synced server in the session wins while the source leaves it alone."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)
    config_path = codex_home / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("gh-mcp", "gh-local"), encoding="utf-8"
    )

    # Only an unrelated server changes in the source.
    (source_home / "config.toml").write_text(
        _GITHUB + '[mcp_servers.docs]\ncommand = "docs-mcp-v2"\n', encoding="utf-8"
    )
    sync_codex_home_mcp_servers(codex_home, source_home)

    servers = _servers(codex_home)
    assert servers["github"]["command"] == "gh-local"
    assert servers["docs"]["command"] == "docs-mcp-v2"


def test_second_sync_is_byte_idempotent(homes: tuple[Path, Path]) -> None:
    """Re-running with an unchanged source rewrites neither file."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)
    config_bytes = (codex_home / "config.toml").read_bytes()
    state_bytes = (codex_home / MCP_SOURCE_STATE_FILENAME).read_bytes()

    sync_codex_home_mcp_servers(codex_home, source_home)

    assert (codex_home / "config.toml").read_bytes() == config_bytes
    assert (codex_home / MCP_SOURCE_STATE_FILENAME).read_bytes() == state_bytes


def test_omnigent_in_source_is_ignored(homes: tuple[Path, Path]) -> None:
    """The generated relay is never synced from the source nor recorded."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(
        '[mcp_servers.omnigent]\ncommand = "/stale"\n' + _GITHUB, encoding="utf-8"
    )
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) == frozenset({"github"})

    assert _servers(codex_home)["omnigent"] == {"command": "/py", "args": ["relay"]}
    assert set(_load(codex_home / MCP_SOURCE_STATE_FILENAME)["mcp_servers"]) == {"github"}


def test_unparsable_source_leaves_everything_untouched(homes: tuple[Path, Path]) -> None:
    """A broken source config is not acted on and no sidecar is written."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text("mcp_servers = [", encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == _PRIVATE_BASE
    assert not (codex_home / MCP_SOURCE_STATE_FILENAME).exists()


def test_unparsable_source_keeps_existing_sidecar(homes: tuple[Path, Path]) -> None:
    """A transient source syntax error does not delete recorded servers."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)
    before = (codex_home / "config.toml").read_bytes()

    (source_home / "config.toml").write_text("oops = [", encoding="utf-8")
    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").read_bytes() == before
    assert "github" in _servers(codex_home)


def test_missing_source_config_skips_sync_and_keeps_state(homes: tuple[Path, Path]) -> None:
    """An absent source config.toml (e.g. mid-save) is unknown, not an empty server set."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    sync_codex_home_mcp_servers(codex_home, source_home)
    config_bytes = (codex_home / "config.toml").read_bytes()
    state_bytes = (codex_home / MCP_SOURCE_STATE_FILENAME).read_bytes()

    (source_home / "config.toml").unlink()
    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").read_bytes() == config_bytes
    assert (codex_home / MCP_SOURCE_STATE_FILENAME).read_bytes() == state_bytes


def test_missing_source_home_or_private_config_is_a_noop(tmp_path: Path) -> None:
    """Nothing is created when either side of the sync does not exist."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) is None
    assert list(codex_home.iterdir()) == []

    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    assert sync_codex_home_mcp_servers(codex_home, tmp_path / "absent") is None
    assert (codex_home / "config.toml").read_text(encoding="utf-8") == _PRIVATE_BASE
    assert not (codex_home / MCP_SOURCE_STATE_FILENAME).exists()


def test_symlinked_private_config_is_not_written(homes: tuple[Path, Path]) -> None:
    """A private config that is a symlink to the user's file is never edited."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").symlink_to(source_home / "config.toml")

    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").is_symlink()
    assert (source_home / "config.toml").read_text(encoding="utf-8") == _GITHUB
    assert not (codex_home / MCP_SOURCE_STATE_FILENAME).exists()


def test_unparsable_private_config_is_left_alone(homes: tuple[Path, Path]) -> None:
    """An invalid private config is left for Codex to report."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text("model = [", encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == "model = ["
    assert not (codex_home / MCP_SOURCE_STATE_FILENAME).exists()


@pytest.mark.parametrize("journal", ["[base]\n[applied]\n[pending]\n", "invalid = ["])
def test_pending_or_unreadable_profile_journal_defers_sync(
    homes: tuple[Path, Path], journal: str
) -> None:
    """The profile step's crash recovery owns the private config until it finishes."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    (codex_home / ".omnigent-config-profile.toml").write_text(journal, encoding="utf-8")

    assert sync_codex_home_mcp_servers(codex_home, source_home) is None

    assert (codex_home / "config.toml").read_text(encoding="utf-8") == _PRIVATE_BASE
    assert not (codex_home / MCP_SOURCE_STATE_FILENAME).exists()


def test_completed_profile_journal_does_not_block_sync(homes: tuple[Path, Path]) -> None:
    """A journal with no pending update is not a reason to skip."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")
    (codex_home / ".omnigent-config-profile.toml").write_text(
        "[base]\n[applied]\n", encoding="utf-8"
    )

    sync_codex_home_mcp_servers(codex_home, source_home)

    assert "github" in _servers(codex_home)


def test_out_of_order_and_quoted_tables_round_trip(homes: tuple[Path, Path]) -> None:
    """Tables split around other sections and dotted names survive the rewrite."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(
        '[mcp_servers.a]\ncommand = "a-mcp"\n\n[projects."/w"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers."docs.v2"]\ncommand = "docs-mcp"\n\n'
        '[mcp_servers."docs.v2".env]\nTOKEN_NAME = "x"\n',
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[mcp_servers.a]\ncommand = "old-a"\n\n[projects."/w"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.omnigent]\ncommand = "/py"\n',
        encoding="utf-8",
    )

    sync_codex_home_mcp_servers(codex_home, source_home)

    config = _load(codex_home / "config.toml")
    assert config["projects"]["/w"]["trust_level"] == "trusted"
    assert config["mcp_servers"]["omnigent"] == {"command": "/py"}
    assert config["mcp_servers"]["a"] == {"command": "a-mcp"}
    assert config["mcp_servers"]["docs.v2"] == {
        "command": "docs-mcp",
        "env": {"TOKEN_NAME": "x"},
    }
    recorded = _load(codex_home / MCP_SOURCE_STATE_FILENAME)["mcp_servers"]
    assert recorded["docs.v2"]["env"] == {"TOKEN_NAME": "x"}


def test_result_survives_mcp_injection_rerun(homes: tuple[Path, Path], tmp_path: Path) -> None:
    """The synced file stays valid TOML with one omnigent table after injection."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB + _DOCS, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    for _ in range(2):
        expected = sync_codex_home_mcp_servers(codex_home, source_home)
        _inject_mcp_server_config(codex_home, tmp_path / "bridge", "/new/python")

    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert text.count("[mcp_servers.omnigent]") == 1
    servers = tomllib.loads(text)["mcp_servers"]
    assert set(servers) == {"omnigent", "github", "docs"}
    assert servers["omnigent"]["command"] == "/new/python"
    assert expected == frozenset({"github", "docs"})
    assert missing_codex_home_mcp_servers(codex_home, expected) == []


def test_sidecar_file_mode_is_private(homes: tuple[Path, Path]) -> None:
    """Recorded tables may carry credentials, so the sidecar is owner-only."""
    codex_home, source_home = homes
    (source_home / "config.toml").write_text(_GITHUB, encoding="utf-8")
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    sync_codex_home_mcp_servers(codex_home, source_home)

    mode = stat.S_IMODE((codex_home / MCP_SOURCE_STATE_FILENAME).stat().st_mode)
    assert mode == 0o600


def test_missing_servers_is_empty_for_empty_expected(homes: tuple[Path, Path]) -> None:
    """Nothing expected means nothing can be missing."""
    codex_home, _ = homes
    (codex_home / "config.toml").write_text(_PRIVATE_BASE, encoding="utf-8")

    assert missing_codex_home_mcp_servers(codex_home, frozenset()) == []


def test_missing_servers_names_absent_expected_servers(homes: tuple[Path, Path]) -> None:
    """Expected servers absent from the private config are listed, sorted."""
    codex_home, _ = homes
    (codex_home / "config.toml").write_text(_PRIVATE_BASE + _DOCS, encoding="utf-8")

    missing = missing_codex_home_mcp_servers(codex_home, frozenset({"slack", "github", "docs"}))

    assert missing == ["github", "slack"]


def test_missing_servers_treats_disabled_server_as_present(homes: tuple[Path, Path]) -> None:
    """``enabled = false`` is a deliberate choice, not a missing server."""
    codex_home, _ = homes
    (codex_home / "config.toml").write_text(
        _PRIVATE_BASE + '[mcp_servers.github]\ncommand = "gh-mcp"\nenabled = false\n',
        encoding="utf-8",
    )

    assert missing_codex_home_mcp_servers(codex_home, frozenset({"github"})) == []


def test_missing_servers_is_empty_for_unreadable_private_config(
    homes: tuple[Path, Path],
) -> None:
    """A config Codex cannot read is reported by Codex, not as missing servers."""
    codex_home, _ = homes
    (codex_home / "config.toml").write_text("model = [", encoding="utf-8")

    assert missing_codex_home_mcp_servers(codex_home, frozenset({"github"})) == []
