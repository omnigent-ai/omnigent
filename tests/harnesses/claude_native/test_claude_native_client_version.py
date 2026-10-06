"""The Default launch never pins a model the installed Claude Code refuses."""

from __future__ import annotations

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import client_version
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.models import model_catalog_store

OLD = "2.1.217"
SCOPE = "catalog-fingerprint"
DAY = 24 * 3600.0


def _rows(default: str = "sonnet[1m]", *, extra: tuple[dict[str, Any], ...] = ()) -> list[dict]:
    """The managed picker the affected hosts saw: 5.5 rows plus older ones."""
    rows: list[dict[str, Any]] = [
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
        {"id": "haiku", "model": "system.ai.claude-haiku-4-5", "displayName": "Haiku 4.5"},
        {
            "id": "opus-4-8[1m]",
            "model": "system.ai.claude-opus-4-8[1m]",
            "displayName": "Opus 4.8 (1M context)",
        },
        *extra,
    ]
    return [{**row, "isDefault": True} if row["id"] == default else row for row in rows]


def _default_ids(rows: list[dict[str, Any]] | None) -> list[str]:
    assert rows is not None
    return [row["id"] for row in rows if row.get("isDefault") is True]


def _refusal(
    client: str = OLD,
    floor: str = "2.1.280",
    *,
    status: int = 400,
    kind: str = "invalid_request_error",
) -> dict[str, object]:
    """The failure evidence ``claude_failure_context`` builds for an API refusal."""
    return {
        "http_status": status,
        "provider_error_type": kind,
        "native_error_message": (
            f"Claude Code {client} does not support this model; version {floor} or newer "
            "is required"
        ),
    }


def _learn(
    model: str = "system.ai.claude-sonnet-5-6[1m]",
    context: dict[str, object] | None = None,
    *,
    installed: str | None = OLD,
    scope: str | None = SCOPE,
    now: float | None = None,
) -> bool:
    return client_version.learn_from_refusal(
        _refusal() if context is None else context,
        installed=installed,
        model=model,
        scope=scope,
        now=now,
    )


# ---------------------------------------------------------------- model keys


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


def test_owned_table_names_the_two_gated_models() -> None:
    assert client_version.min_client_version("system.ai.claude-opus-5-5[1m]") == "2.1.280"
    assert client_version.min_client_version("claude-sonnet-5-5") == "2.1.280"
    assert client_version.min_client_version("claude-opus-5") is None
    assert client_version.min_client_version("claude-haiku-4-5") is None


# ------------------------------------------------- what counts as a refusal


def test_refusal_reads_the_client_and_the_floor() -> None:
    assert client_version.client_refusal(_refusal()) == client_version.ClientRefusal(
        OLD, "2.1.280"
    )


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(None, id="no-evidence"),
        pytest.param({}, id="empty-evidence"),
        pytest.param(
            {"native_error_message": _refusal()["native_error_message"]}, id="prose-no-status"
        ),
        pytest.param(_refusal(status=429), id="rate-limit-status"),
        pytest.param(_refusal(status=500), id="server-status"),
        pytest.param(_refusal(kind="overloaded_error"), id="other-error-type"),
        pytest.param({**_refusal(), "provider_error_type": None}, id="no-error-type"),
        pytest.param(
            {
                **_refusal(),
                "native_error_message": "Please update: "
                + str(_refusal()["native_error_message"]),
            },
            id="refusal-sentence-inside-other-text",
        ),
        pytest.param(
            _refusal(client="2.1.300", floor="2.1.280"), id="floor-not-newer-than-client"
        ),
        pytest.param(_refusal(client="2.1.280", floor="2.1.280"), id="floor-equals-client"),
        pytest.param(_refusal(floor="2.1.280.1"), id="four-part-floor"),
        pytest.param(_refusal(client="2.1.217.1"), id="four-part-client"),
        pytest.param({**_refusal(), "native_error_message": 7}, id="message-not-text"),
    ],
)
def test_only_the_structured_refusal_counts(context: dict[str, object] | None) -> None:
    assert client_version.client_refusal(context) is None


# -------------------------------------------------------- learning a floor


def test_a_learned_floor_holds_for_its_catalog_and_the_refused_release() -> None:
    assert _learn()
    floors = client_version.learned_min_client_versions(SCOPE, OLD)
    assert floors == {"sonnet-5-6": "2.1.280"}
    assert client_version.min_client_version("claude-sonnet-5-6", floors) == "2.1.280"
    assert client_version.min_client_version("claude-sonnet-5-6") is None


def test_the_floor_file_keeps_the_release_that_was_refused_and_when() -> None:
    assert _learn(now=1_000.0)
    (record,) = json.loads(client_version._floors_path().read_text())["floors"]
    assert record == {
        "scope": SCOPE,
        "model": "sonnet-5-6",
        "floor": "2.1.280",
        "refused_client": OLD,
        "learned_at": 1_000.0,
    }


def test_a_learned_floor_does_not_follow_another_catalog() -> None:
    assert _learn()
    assert client_version.learned_min_client_versions("another-provider", OLD) == {}
    assert client_version.learned_min_client_versions(None, OLD) == {}


def test_a_learned_floor_ends_when_the_installed_release_changes() -> None:
    assert _learn()
    assert client_version.learned_min_client_versions(SCOPE, "2.1.250") == {}
    assert client_version.learned_min_client_versions(SCOPE, None) == {}


def test_a_learned_floor_expires_after_thirty_days() -> None:
    assert _learn(now=1_000_000.0)
    floors = client_version.learned_min_client_versions
    assert floors(SCOPE, OLD, now=1_000_000.0 + 29 * DAY) == {"sonnet-5-6": "2.1.280"}
    assert floors(SCOPE, OLD, now=1_000_000.0 + 31 * DAY) == {}
    # A record dated far in the future is as suspect as an old one.
    assert floors(SCOPE, OLD, now=1_000_000.0 - 3 * DAY) == {}


def test_relearning_a_model_replaces_its_record() -> None:
    assert _learn(context=_refusal(floor="2.1.280"), now=1_000.0)
    assert _learn(context=_refusal(floor="2.1.290"), now=2_000.0)
    records = json.loads(client_version._floors_path().read_text())["floors"]
    assert [(r["floor"], r["learned_at"]) for r in records] == [("2.1.290", 2_000.0)]


def test_an_expired_record_is_dropped_on_the_next_write() -> None:
    assert _learn(model="claude-opus-5-6", now=1_000.0)
    assert _learn(model="claude-sonnet-5-6", now=1_000.0 + 40 * DAY)
    records = json.loads(client_version._floors_path().read_text())["floors"]
    assert [record["model"] for record in records] == ["sonnet-5-6"]


# ----------------- prose, other errors and bogus floors never teach anything


def _floor_file_is_untouched() -> bool:
    return not client_version._floors_path().exists()


def test_assistant_prose_never_teaches_a_floor() -> None:
    prose = {
        "native_error_message": (
            "Claude Code 2.1.217 does not support this model; version 99.0.0 or newer is required"
        )
    }
    assert not _learn(context=prose)
    assert _floor_file_is_untouched()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 529])
def test_a_refusal_shaped_message_on_another_status_teaches_nothing(status: int) -> None:
    assert not _learn(context=_refusal(status=status))
    assert _floor_file_is_untouched()


def test_a_bogus_floor_from_another_client_teaches_nothing() -> None:
    assert not _learn(context=_refusal(client="1.0.0", floor="99.0.0"))
    assert not _learn(context=_refusal(client="2.1.300", floor="99.0.0"))
    assert _floor_file_is_untouched()


def test_a_bogus_floor_stays_inside_its_catalog_release_and_month() -> None:
    assert _learn(model="claude-opus-4-8", context=_refusal(floor="99.0.0"), now=5_000.0)
    assert client_version.learned_min_client_versions(SCOPE, OLD, now=5_000.0) == {
        "opus-4-8": "99.0.0"
    }
    assert client_version.learned_min_client_versions(SCOPE, "2.1.291", now=5_000.0) == {}
    assert client_version.learned_min_client_versions(SCOPE, OLD, now=5_000.0 + 31 * DAY) == {}


def test_the_refused_release_must_be_the_installed_one() -> None:
    assert not _learn(installed="2.1.291")
    assert not _learn(installed=None)
    assert _floor_file_is_untouched()


def test_the_native_cli_must_agree_with_the_refused_release() -> None:
    assert not _learn(context={**_refusal(), "native_cli_version": "2.1.291"})
    assert _floor_file_is_untouched()
    assert _learn(context={**_refusal(), "native_cli_version": OLD})


@pytest.mark.parametrize(
    "kwargs",
    [{"model": None}, {"model": ""}, {"model": "sonnet[1m]"}, {"model": "opus"}, {"scope": None}],
)
def test_an_unknown_model_alias_or_catalog_teaches_nothing(kwargs: dict[str, Any]) -> None:
    assert not _learn(**kwargs)
    assert _floor_file_is_untouched()


def test_the_opt_out_stops_learning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client_version.FLOOR_ENV_VAR, "0")
    assert not _learn()
    assert _floor_file_is_untouched()


# ------------------------------------------------- the floor file's robustness


def _record_json(**overrides: object) -> str:
    """A one-record floors file whose record is valid except for *overrides*."""
    record: dict[str, object] = {
        "scope": "s",
        "model": "m",
        "floor": "2.1.9",
        "refused_client": "2.1.1",
        "learned_at": 1,
    }
    record.update(overrides)
    return json.dumps({"floors": [record]})


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"floors": {"opus-5-6": "2.1.300"}}',
        '{"floors": ["nope", 7, null]}',
        _record_json(floor="soon"),
        _record_json(floor="1.0.0"),
        _record_json(learned_at=True),
        _record_json(learned_at="yesterday"),
        _record_json(scope=""),
        json.dumps({"floors": [{"scope": "s", "model": "m", "floor": "2.1.9"}]}),
    ],
)
def test_a_damaged_floor_file_is_ignored_and_rewritten(content: str) -> None:
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert client_version.learned_min_client_versions("s", "2.1.1") == {}
    assert _learn()
    assert client_version.learned_min_client_versions(SCOPE, OLD) == {"sonnet-5-6": "2.1.280"}


def test_valid_entries_survive_beside_damaged_ones() -> None:
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = 1_000.0
    path.write_text(
        json.dumps(
            {
                "floors": [
                    {
                        "scope": "s",
                        "model": "a",
                        "floor": "2.1.9",
                        "refused_client": "2.1.1",
                        "learned_at": now,
                    },
                    {
                        "scope": "s",
                        "model": "b",
                        "floor": "soon",
                        "refused_client": "2.1.1",
                        "learned_at": now,
                    },
                ]
            }
        )
    )
    assert client_version.learned_min_client_versions("s", "2.1.1", now=now) == {"a": "2.1.9"}


def test_the_floor_file_stays_tiny() -> None:
    for generation in range(client_version._MAX_LEARNED_FLOORS + 5):
        assert _learn(model=f"claude-opus-9-{generation}", now=1_000.0 + generation)
    learned = client_version.learned_min_client_versions(
        SCOPE, OLD, now=1_000.0 + client_version._MAX_LEARNED_FLOORS
    )
    assert len(learned) == client_version._MAX_LEARNED_FLOORS
    assert "opus-9-0" not in learned
    assert f"opus-9-{client_version._MAX_LEARNED_FLOORS + 4}" in learned


def test_concurrent_lessons_are_all_kept() -> None:
    models = [f"claude-opus-8-{index}" for index in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda model: _learn(model=model), models))
    assert all(results)
    assert len(client_version.learned_min_client_versions(SCOPE, OLD)) == len(models)


def test_floor_file_lives_beside_the_other_claude_native_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))
    assert _learn()
    assert (tmp_path / "model-client-floors.json").is_file()
    assert [path for path in tmp_path.iterdir() if path.suffix == ".tmp"] == []


# ---------------------------------------------------------------- the demotion


def _floor(
    rows: list[dict[str, Any]], installed: str | None = OLD, scope: str | None = SCOPE
) -> client_version.FlooredRows:
    return client_version.floor_catalog_default(rows, installed=installed, scope=scope)


@pytest.mark.parametrize(
    ("default", "wanted"),
    [
        ("sonnet[1m]", "Sonnet 5.5 (1M context)"),
        ("opus[1m]", "Opus 5.5 (1M context)"),
    ],
)
def test_old_client_moves_the_default_to_a_callable_row(default: str, wanted: str) -> None:
    rows = _rows(default)
    floored = _floor(rows)
    assert _default_ids(floored) == ["opus-4-8[1m]"]
    assert [row["id"] for row in floored] == [row["id"] for row in rows]
    demotion = floored.demotion
    assert demotion is not None
    assert (demotion.cli_version, demotion.min_version) == (OLD, "2.1.280")
    assert demotion.wanted["id"] == default
    assert demotion.chosen_model == "system.ai.claude-opus-4-8[1m]"
    # The stored rows are untouched and the demoted model stays a listed row.
    assert _default_ids(rows) == [default]
    assert {row["model"] for row in floored} == {row["model"] for row in rows}
    assert (floored.cli_version, floored.scope) == (OLD, SCOPE)


def test_the_replacement_prefers_the_demoted_models_own_family() -> None:
    sonnet = {"id": "sonnet-4-6", "model": "claude-sonnet-4-6", "displayName": "Sonnet 4.6"}
    floored = _floor(_rows("sonnet[1m]", extra=(sonnet,)))
    assert _default_ids(floored) == ["sonnet-4-6"]
    # No runnable row of the family: the newest of any family takes over.
    assert _default_ids(_floor(_rows("sonnet[1m]"))) == ["opus-4-8[1m]"]
    # Opus 5.5 stays within Opus even with a newer-looking Sonnet listed.
    newer = {"id": "sonnet-4-9", "model": "claude-sonnet-4-9", "displayName": "Sonnet 4.9"}
    assert _default_ids(_floor(_rows("opus[1m]", extra=(newer,)))) == ["opus-4-8[1m]"]


def test_the_replacement_is_the_newest_generation_and_non_claude_rows_rank_last() -> None:
    rows = [
        _rows()[0],
        {"id": "gpt", "model": "system.ai.gpt-5-6", "displayName": "GPT 5.6"},
        {"id": "old", "model": "claude-fable-4-20250514", "displayName": "Fable 4"},
        {"id": "new", "model": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5"},
        {"id": "twin", "model": "claude-opus-4-5", "displayName": "Opus 4.5"},
    ]
    # 4.5 ties between haiku and opus: the catalog's own order breaks it.
    assert _default_ids(_floor(rows)) == ["new"]
    assert _default_ids(_floor([rows[0], rows[1]])) == ["gpt"]


def test_client_at_the_floor_leaves_the_catalog_alone() -> None:
    floored = _floor(_rows(), "2.1.280")
    assert floored == _rows()
    assert floored.demotion is None
    assert _floor(_rows(), "2.1.291").demotion is None


def test_unknown_client_leaves_the_catalog_alone() -> None:
    floored = _floor(_rows(), None)
    assert floored == _rows()
    assert floored.demotion is None
    assert floored.cli_version is None


def test_default_with_no_floor_is_left_alone() -> None:
    rows = _rows("haiku")
    assert _floor(rows, "2.1.100") == rows
    assert _floor([], "2.1.100") == []


def test_nothing_callable_keeps_the_default() -> None:
    assert _floor(_rows()[:2]).demotion is None


def test_opting_out_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client_version.FLOOR_ENV_VAR, "0")
    floored = _floor(_rows())
    assert floored == _rows()
    assert floored.demotion is None
    monkeypatch.setenv(client_version.FLOOR_ENV_VAR, "1")
    assert _floor(_rows()).demotion is not None


def test_a_learned_floor_demotes_a_model_the_table_does_not_know() -> None:
    rows = [
        {"id": "next", "model": "claude-sonnet-5-6", "isDefault": True},
        {"id": "haiku", "model": "claude-haiku-4-5"},
    ]
    assert _floor(rows).demotion is None
    assert _learn(model="claude-sonnet-5-6")
    demotion = _floor(rows).demotion
    assert demotion is not None
    assert demotion.min_version == "2.1.280"
    assert _default_ids(_floor(rows)) == ["haiku"]


def test_a_learned_floor_is_not_applied_to_another_catalog_release_or_client() -> None:
    rows = [
        {"id": "next", "model": "claude-sonnet-5-6", "isDefault": True},
        {"id": "haiku", "model": "claude-haiku-4-5"},
    ]
    assert _learn(model="claude-sonnet-5-6")
    assert _floor(rows, scope="elsewhere").demotion is None
    assert _floor(rows, installed="2.1.250").demotion is None
    assert _floor(rows, installed="2.1.280").demotion is None


def test_the_notice_names_both_models_and_how_to_fix_it() -> None:
    demotion = _floor(_rows("sonnet[1m]")).demotion
    assert demotion is not None
    assert demotion.notice() == (
        "Claude Code 2.1.217 can't run Sonnet 5.5 (1M context); it needs 2.1.280 or newer. "
        "This session uses Opus 4.8 (1M context) instead. Update Claude Code on the host "
        "(for example `claude update`) to use it."
    )
    assert ") (" not in demotion.notice()


def test_the_notice_key_is_stable_per_session_release_and_model() -> None:
    demotion = _floor(_rows()).demotion
    assert demotion is not None
    key = demotion.notice_source_id("sess-1")
    assert key == demotion.notice_source_id("sess-1")
    assert key != demotion.notice_source_id("sess-2")
    assert OLD in key and demotion.chosen_model in key and "sess-1" in key
    assert len(demotion.notice_source_id("s" * 400)) <= 256


# ------------------------------------------------- what every catalog reader sees


def _store(
    version: str | None, *, rows: list[dict[str, Any]] | None = None, config: object = None
) -> str:
    """Store the affected host's picker, as the probe would, and return its fingerprint."""
    fingerprint = claude_native.claude_catalog_fingerprint(config)  # type: ignore[arg-type]
    model_catalog_store.write_catalog(
        "claude-native",
        fingerprint,
        model_catalog_store.CatalogRows(
            rows if rows is not None else _rows(),
            meta={"cli_version": version} if version else {},
        ),
    )
    return fingerprint


@pytest.fixture(autouse=True)
def _no_probe_or_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the store; a fresh stored catalog must be served without a probe or process."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    def _spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a catalog read spawned a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", _spawn)
    monkeypatch.setattr(subprocess, "Popen", _spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


async def test_launch_catalog_serves_the_demoted_default_to_every_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = _store(OLD)

    async def _probe(config: object) -> list[dict[str, Any]]:
        return model_catalog_store.CatalogRows(_rows(), meta={"cli_version": OLD})

    monkeypatch.setattr(claude_native, "claude_model_catalog", _probe)
    served = await claude_native.claude_launch_catalog(None)
    assert _default_ids(served) == ["opus-4-8[1m]"]
    assert _default_ids(await claude_native.claude_reprobed_launch_catalog(None)) == [
        "opus-4-8[1m]"
    ]
    assert served is not None
    demotion = getattr(served, "demotion", None)
    assert demotion is not None and demotion.chosen_model == "system.ai.claude-opus-4-8[1m]"
    assert (getattr(served, "cli_version", None), getattr(served, "scope", None)) == (
        OLD,
        fingerprint,
    )
    # The store keeps the probe's verbatim answer, so an upgrade needs no re-probe.
    stored = claude_native.stored_claude_catalog_rows(None)
    assert stored == _rows()
    assert _default_ids(stored) == ["sonnet[1m]"]
    _store("2.1.291")
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["sonnet[1m]"]


async def test_a_catalog_probed_without_a_release_is_served_as_stored() -> None:
    fingerprint = _store(None)
    served = await claude_native.claude_launch_catalog(None)
    assert served == _rows()
    assert getattr(served, "demotion", "missing") is None
    assert getattr(served, "cli_version", "missing") is None
    assert getattr(served, "scope", None) == fingerprint


async def test_launch_catalog_leaves_a_config_pinned_default_alone() -> None:
    config = claude_native.ClaudeNativeUcodeConfig(env={}, model="system.ai.claude-sonnet-5-5[1m]")
    _store(OLD, config=config)
    served = await claude_native.claude_launch_catalog(config)
    assert _default_ids(served) == ["sonnet[1m]"]
    assert getattr(served, "demotion", "missing") is None


async def test_a_failing_floor_check_serves_the_stored_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(OLD)

    def _broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("floor check blew up")

    monkeypatch.setattr(claude_native, "floor_catalog_default", _broken)
    served = await claude_native.claude_launch_catalog(None)
    assert served == _rows()
    assert getattr(served, "demotion", "missing") is None


async def test_a_refusal_demotes_the_model_on_the_next_catalog_read() -> None:
    rows = [
        {"id": "next", "model": "system.ai.claude-sonnet-5-6[1m]", "isDefault": True},
        {"id": "haiku", "model": "system.ai.claude-haiku-4-5"},
    ]
    fingerprint = _store(OLD, rows=rows)
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["next"]

    assert client_version.learn_from_refusal(
        _refusal(),
        installed=OLD,
        model="system.ai.claude-sonnet-5-6[1m]",
        scope=fingerprint,
    )

    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["haiku"]
    # ... and only while that client is still the one installed.
    _store("2.1.250", rows=rows)
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["next"]


async def test_a_damaged_floor_file_leaves_the_owned_table_in_force() -> None:
    _store(OLD)
    path = client_version._floors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00{garbage")
    assert _default_ids(await claude_native.claude_launch_catalog(None)) == ["opus-4-8[1m]"]


async def test_the_probed_release_is_stored_with_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _probe(config: object) -> claude_native.ClaudeModelProbe:
        return claude_native.ClaudeModelProbe(
            alias_rows=[{"id": "haiku", "model": "system.ai.claude-haiku-4-5"}],
            default_model="system.ai.claude-haiku-4-5",
            cli_version=OLD,
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _probe)
    served = await claude_native.claude_launch_catalog(None)
    assert _default_ids(served) == ["haiku"]
    fingerprint = claude_native.claude_catalog_fingerprint(None)
    assert model_catalog_store.read_catalog_meta("claude-native", fingerprint) == {
        "cli_version": OLD
    }


async def test_a_probe_without_a_release_stores_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _probe(config: object) -> claude_native.ClaudeModelProbe:
        return claude_native.ClaudeModelProbe(
            alias_rows=[{"id": "haiku", "model": "system.ai.claude-haiku-4-5"}],
            default_model="system.ai.claude-haiku-4-5",
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _probe)
    await claude_native.claude_launch_catalog(None)
    fingerprint = claude_native.claude_catalog_fingerprint(None)
    assert model_catalog_store.read_catalog_meta("claude-native", fingerprint) == {}
