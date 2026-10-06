"""The Default launch never pins a model the installed Claude Code refuses."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import client_version
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.models import model_catalog_store

# Captured before any fixture swaps the module attribute.
REAL_INSTALLED_CLI_VERSION = client_version.installed_cli_version

REFUSAL = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":'
    '"Claude Code 2.1.217 does not support this model; version 2.1.280 or newer is '
    'required"},"request_id":"req_1"}'
)


def _rows(default: str = "sonnet[1m]") -> list[dict[str, Any]]:
    """The managed picker the affected hosts saw: 5.5 rows plus older ones."""
    rows = [
        {
            "id": "sonnet[1m]",
            "model": "system.ai.claude-sonnet-5-5[1m]",
            "displayName": "Sonnet 5.5 (1M context)",
        },
        {
            "id": "opus[1m]",
            "model": "system.ai.claude-opus-5-5[1m]",
            "displayName": "Opus 5.5 (1M context)",
        },
        {
            "id": "haiku",
            "model": "system.ai.claude-haiku-4-5",
            "displayName": "Haiku 4.5",
        },
        {
            "id": "opus-4-8[1m]",
            "model": "system.ai.claude-opus-4-8[1m]",
            "displayName": "Opus 4.8 (1M context)",
        },
    ]
    return [{**row, "isDefault": True} if row["id"] == default else row for row in rows]


def _default_ids(rows: list[dict[str, Any]] | None) -> list[str]:
    assert rows is not None
    return [row["id"] for row in rows if row.get("isDefault") is True]


def _install(monkeypatch: pytest.MonkeyPatch, version: str | None) -> list[int]:
    """Stub the installed release; the returned list counts how often it is read."""
    reads: list[int] = []

    def _read() -> str | None:
        reads.append(1)
        return version

    monkeypatch.setattr(client_version, "installed_cli_version", _read)
    return reads


@pytest.fixture(autouse=True)
def _fresh_version_cache() -> None:
    client_version._version_cache.clear()


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "system.ai.claude-opus-5-5[1m]",
        "databricks-claude-opus-5-5",
        "anthropic/claude-opus-5-5",
        "us.anthropic.claude-opus-5-5-v1:0",
        "CLAUDE-OPUS-5-5-20261001",
    ],
)
def test_model_floor_key_folds_gateway_prefix_marker_and_date(model: str) -> None:
    assert client_version.model_floor_key(model) == "opus-5-5"


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("claude-opus-5", "opus-5"),
        ("system.ai.claude-opus-4-8[1m]", "opus-4-8"),
        ("claude-sonnet-4-20250514", "sonnet-4"),
        ("claude-haiku-4-5-20251001", "haiku-4-5"),
        ("system.ai.glm-5-3", "glm-5-3"),
        ("opus[1m]", "opus"),
        ("", None),
    ],
)
def test_model_floor_key_other_shapes(model: str, key: str | None) -> None:
    assert client_version.model_floor_key(model) == key


def test_builtin_table_names_the_two_gated_models() -> None:
    assert client_version.min_client_version("system.ai.claude-opus-5-5[1m]") == "2.1.280"
    assert client_version.min_client_version("claude-sonnet-5-5") == "2.1.280"
    assert client_version.min_client_version("claude-opus-5") is None
    assert client_version.min_client_version("claude-haiku-4-5") is None


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("2.1.217 (Claude Code)\n", "2.1.217"),
        ("isaac 0.5.1\n2.1.217 (Claude Code)", "2.1.217"),
        ("isaac 0.5.1\n", None),
        ("2.1.217\n", None),
        ("", None),
    ],
)
def test_parse_cli_version_reads_only_the_claude_code_line(text: str, version: str | None) -> None:
    assert client_version.parse_cli_version(text) == version


def test_unsupported_model_min_version_reads_the_refusal() -> None:
    assert client_version.unsupported_model_min_version(REFUSAL) == "2.1.280"
    assert client_version.unsupported_model_min_version("API Error: 500 overloaded") is None
    assert client_version.unsupported_model_min_version(None) is None


# ------------------------------------------------------ learned-floor file


def test_learned_floor_round_trips_and_only_rises() -> None:
    assert client_version.record_min_client_version("system.ai.claude-opus-5-6[1m]", "2.1.300")
    assert client_version.learned_min_client_versions() == {"opus-5-6": "2.1.300"}
    assert not client_version.record_min_client_version("claude-opus-5-6", "2.1.290")
    assert client_version.record_min_client_version("claude-opus-5-6", "2.1.310")
    assert client_version.learned_min_client_versions() == {"opus-5-6": "2.1.310"}
    assert client_version.min_client_version("claude-opus-5-6") is None
    learned = client_version.learned_min_client_versions()
    assert client_version.min_client_version("claude-opus-5-6", learned) == "2.1.310"


@pytest.mark.parametrize(
    ("model", "version"),
    [("opus[1m]", "2.1.300"), ("sonnet", "2.1.300"), ("", "2.1.300"), ("claude-opus-5-6", "soon")],
)
def test_learned_floor_refuses_aliases_and_junk(model: str, version: str) -> None:
    assert not client_version.record_min_client_version(model, version)
    assert client_version.learned_min_client_versions() == {}


def test_learned_floor_file_stays_tiny() -> None:
    for generation in range(client_version._MAX_LEARNED_FLOORS + 5):
        client_version.record_min_client_version(f"claude-opus-9-{generation}", "2.1.300")
    learned = client_version.learned_min_client_versions()
    assert len(learned) == client_version._MAX_LEARNED_FLOORS
    assert "opus-9-0" not in learned
    assert f"opus-9-{client_version._MAX_LEARNED_FLOORS + 4}" in learned


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"floors": []}',
        '{"floors": {"opus-5-6": "soon", "sonnet-5-6": 7}}',
        '{"floors": {"opus-5-6": "2.1.300"}',
    ],
)
def test_corrupt_floor_file_is_ignored_and_rewritten(content: str) -> None:
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert client_version.learned_min_client_versions() == {}
    assert client_version.record_min_client_version("claude-opus-5-6", "2.1.300")
    assert client_version.learned_min_client_versions() == {"opus-5-6": "2.1.300"}


def test_floor_file_keeps_valid_entries_beside_damaged_ones() -> None:
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"floors": {"opus-5-6": "2.1.300", "sonnet-5-6": "soon"}}')
    assert client_version.learned_min_client_versions() == {"opus-5-6": "2.1.300"}


# ------------------------------------------------------- the installed release


def _fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str) -> Path:
    """A ``claude`` that prints *output* and logs each run beside itself."""
    script = tmp_path / "claude"
    script.write_text(f'#!/bin/sh\necho ran >> "{tmp_path}/runs"\nprintf \'%s\\n\' "{output}"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        "omnigent.claude_launcher.resolve_claude_launch",
        lambda command, args: (str(script), list(args)),
    )
    return script


def _runs(tmp_path: Path) -> int:
    runs = tmp_path / "runs"
    return len(runs.read_text().splitlines()) if runs.exists() else 0


@pytest.mark.posix_only
def test_installed_release_is_read_once_per_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _fake_claude(tmp_path, monkeypatch, "2.1.217 (Claude Code)")
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.217"
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.217"
    assert _runs(tmp_path) == 1
    # An upgrade in place changes the binary's identity, so it is read again.
    script.write_text(script.read_text().replace("2.1.217", "2.1.290 "))
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.290"
    assert _runs(tmp_path) == 2


@pytest.mark.posix_only
def test_release_is_read_again_after_a_while_for_launchers_that_upgrade_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "claude"
    script.write_text(f'#!/bin/sh\necho ran >> "{tmp_path}/runs"\ncat "{tmp_path}/out"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "out").write_text("2.1.217 (Claude Code)\n")
    monkeypatch.setattr(
        "omnigent.claude_launcher.resolve_claude_launch",
        lambda command, args: (str(script), list(args)),
    )
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.217"
    # The executable is unchanged, so the upgrade is invisible until the re-read.
    (tmp_path / "out").write_text("2.1.291 (Claude Code)\n")
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.217"
    monkeypatch.setattr(client_version, "_VERSION_REREAD_S", 0.0)
    assert REAL_INSTALLED_CLI_VERSION() == "2.1.291"
    assert _runs(tmp_path) == 2


@pytest.mark.posix_only
def test_unreadable_release_is_unknown_and_retried_only_after_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, "isaac 0.5.1")
    assert REAL_INSTALLED_CLI_VERSION() is None
    assert REAL_INSTALLED_CLI_VERSION() is None
    assert _runs(tmp_path) == 1
    monkeypatch.setattr(client_version, "_UNKNOWN_VERSION_RETRY_S", 0.0)
    assert REAL_INSTALLED_CLI_VERSION() is None
    assert _runs(tmp_path) == 2


@pytest.mark.posix_only
def test_missing_binary_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.claude_launcher.resolve_claude_launch",
        lambda command, args: (str(tmp_path / "absent"), list(args)),
    )
    assert REAL_INSTALLED_CLI_VERSION() is None


# ---------------------------------------------------------------- the demotion


@pytest.mark.parametrize(
    ("default", "wanted"),
    [
        ("sonnet[1m]", "Sonnet 5.5 (1M context)"),
        ("opus[1m]", "Opus 5.5 (1M context)"),
    ],
)
async def test_old_client_moves_the_default_to_the_newest_callable_row(
    monkeypatch: pytest.MonkeyPatch, default: str, wanted: str
) -> None:
    _install(monkeypatch, "2.1.217")
    rows = _rows(default)
    demotion = await client_version.demote_default_for_installed_client(rows)
    assert demotion is not None
    assert _default_ids(demotion.rows) == ["opus-4-8[1m]"]
    assert [row["id"] for row in demotion.rows] == [row["id"] for row in rows]
    assert demotion.cli_version == "2.1.217"
    assert demotion.min_version == "2.1.280"
    assert demotion.wanted["id"] == default
    assert demotion.chosen_model == "system.ai.claude-opus-4-8[1m]"
    assert demotion.notice() == (
        f"Claude Code 2.1.217 can't run {wanted} (needs 2.1.280 or newer), "
        "so this session uses Opus 4.8 (1M context). Run `claude update` to use it."
    )
    # The stored rows are untouched and the demoted model stays a listed row.
    assert _default_ids(rows) == [default]
    assert {row["model"] for row in demotion.rows} == {row["model"] for row in rows}


async def test_client_at_the_floor_leaves_the_catalog_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "2.1.280")
    assert await client_version.demote_default_for_installed_client(_rows()) is None


async def test_unknown_client_leaves_the_catalog_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None)
    assert await client_version.demote_default_for_installed_client(_rows()) is None


async def test_default_with_no_floor_never_reads_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _install(monkeypatch, "2.1.100")
    rows = _rows()
    rows[0]["isDefault"] = False
    rows[2]["isDefault"] = True
    assert await client_version.demote_default_for_installed_client(rows) is None
    assert await client_version.demote_default_for_installed_client([]) is None
    assert reads == []


async def test_nothing_callable_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "2.1.217")
    assert await client_version.demote_default_for_installed_client(_rows()[:2]) is None


async def test_replacement_is_the_newest_claude_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "2.1.217")
    rows = [
        _rows()[0],
        {"id": "gpt", "model": "system.ai.gpt-5-6", "displayName": "GPT 5.6"},
        {"id": "old", "model": "claude-sonnet-4-20250514", "displayName": "Sonnet 4"},
        {"id": "new", "model": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5"},
        {"id": "twin", "model": "claude-opus-4-5", "displayName": "Opus 4.5"},
    ]
    demotion = await client_version.demote_default_for_installed_client(rows)
    assert demotion is not None
    # 4.5 ties between haiku and opus: the catalog's own order breaks it.
    assert _default_ids(demotion.rows) == ["new"]


async def test_learned_floor_demotes_a_model_the_table_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "2.1.217")
    rows = [
        {"id": "next", "model": "claude-sonnet-5-6", "isDefault": True},
        {"id": "haiku", "model": "claude-haiku-4-5"},
    ]
    assert await client_version.demote_default_for_installed_client(rows) is None
    client_version.record_min_client_version("claude-sonnet-5-6", "2.1.300")
    demotion = await client_version.demote_default_for_installed_client(rows)
    assert demotion is not None
    assert demotion.min_version == "2.1.300"
    assert _default_ids(demotion.rows) == ["haiku"]


async def test_learned_floor_never_demotes_a_client_that_meets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "2.1.300")
    client_version.record_min_client_version("claude-sonnet-5-6", "2.1.300")
    rows = [
        {"id": "next", "model": "claude-sonnet-5-6", "isDefault": True},
        {"id": "haiku", "model": "claude-haiku-4-5"},
    ]
    assert await client_version.demote_default_for_installed_client(rows) is None


async def test_corrupt_floor_file_leaves_the_table_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "2.1.217")
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00{garbage")
    demotion = await client_version.demote_default_for_installed_client(_rows())
    assert demotion is not None
    assert _default_ids(demotion.rows) == ["opus-4-8[1m]"]


# ---------------------------------------------- what every catalog reader sees


@pytest.fixture
def seeded_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Store the affected host's picker under the no-provider fingerprint."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    rows = _rows()
    model_catalog_store.write_catalog(
        "claude-native", claude_native.claude_catalog_fingerprint(None), rows
    )

    async def _probe(config: object) -> list[dict[str, Any]]:
        return _rows()

    monkeypatch.setattr(claude_native, "claude_model_catalog", _probe)
    return rows


async def test_launch_catalog_serves_the_demoted_default_to_every_reader(
    seeded_store: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "2.1.217")
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["opus-4-8[1m]"]
    assert _default_ids(await claude_native.claude_reprobed_launch_catalog(None)) == [
        "opus-4-8[1m]"
    ]
    # The store keeps the probe's verbatim answer, so an upgrade needs no re-probe.
    stored = claude_native.stored_claude_catalog_rows(None)
    assert stored == seeded_store
    assert _default_ids(stored) == ["sonnet[1m]"]
    demotion = await claude_native.claude_default_model_demotion(None)
    assert demotion is not None
    assert demotion.chosen_model == "system.ai.claude-opus-4-8[1m]"
    _install(monkeypatch, "2.1.291")
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["sonnet[1m]"]
    assert await claude_native.claude_default_model_demotion(None) is None


async def test_a_failing_floor_check_serves_the_stored_catalog(
    seeded_store: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _broken(rows: object) -> None:
        raise RuntimeError("version probe blew up")

    monkeypatch.setattr(claude_native, "demote_default_for_installed_client", _broken)
    assert await claude_native.claude_launch_catalog(None) == seeded_store
    assert await claude_native.claude_default_model_demotion(None) is None


async def test_launch_catalog_leaves_a_config_pinned_default_alone(
    seeded_store: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "2.1.217")
    config = claude_native.ClaudeNativeUcodeConfig(env={}, model="system.ai.claude-sonnet-5-5[1m]")
    model_catalog_store.write_catalog(
        "claude-native", claude_native.claude_catalog_fingerprint(config), seeded_store
    )
    assert _default_ids(await claude_native.claude_launch_catalog(config)) == ["sonnet[1m]"]
    assert await claude_native.claude_default_model_demotion(config) is None


async def test_a_refusal_demotes_the_model_on_the_next_catalog_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    rows = [
        {"id": "next", "model": "system.ai.claude-sonnet-5-6[1m]", "isDefault": True},
        {"id": "haiku", "model": "system.ai.claude-haiku-4-5"},
    ]
    model_catalog_store.write_catalog(
        "claude-native", claude_native.claude_catalog_fingerprint(None), rows
    )
    _install(monkeypatch, "2.1.217")
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["next"]

    floor = client_version.unsupported_model_min_version(REFUSAL)
    assert floor is not None
    client_version.record_min_client_version("system.ai.claude-sonnet-5-6[1m]", floor)

    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["haiku"]


def test_floor_file_lives_beside_the_other_claude_native_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))
    assert client_version.record_min_client_version("claude-opus-5-6", "2.1.300")
    written = tmp_path / "model-client-floors.json"
    assert json.loads(written.read_text()) == {"floors": {"opus-5-6": "2.1.300"}}
    assert [name for name in os.listdir(tmp_path) if name.endswith(".tmp")] == []
