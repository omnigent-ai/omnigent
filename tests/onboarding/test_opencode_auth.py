"""Tests for opencode-native credential reporting (``opencode_auth.py``)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import omnigent.onboarding.opencode_auth as oc


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_DATA_HOME at a tmp dir and clear provider env keys."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    for _provider_id, _label, var in oc._ENV_PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)


def _write_auth(tmp_path: Path, providers: dict[str, object]) -> None:
    path = oc.opencode_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(providers), encoding="utf-8")


def test_auth_path_honors_xdg_data_home(tmp_path: Path) -> None:
    assert oc.opencode_auth_path() == tmp_path / "share" / "opencode" / "auth.json"


def test_stored_providers_reads_auth_json_keys(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}, "openai": {"type": "oauth"}})
    assert set(oc._stored_providers()) == {"anthropic", "openai"}


def test_stored_providers_ignores_empty_entries(tmp_path: Path) -> None:
    """An empty provider object is config shape, not a usable stored credential."""
    _write_auth(
        tmp_path,
        {
            "anthropic": {"type": "api", "key": "x"},
            "openai": {},
            "groq": "",
        },
    )
    assert oc._stored_providers() == ("anthropic",)


def test_stored_providers_empty_when_missing_or_invalid(tmp_path: Path) -> None:
    assert oc._stored_providers() == ()  # no file
    oc.opencode_auth_path().parent.mkdir(parents=True, exist_ok=True)
    oc.opencode_auth_path().write_text("not json", encoding="utf-8")
    assert oc._stored_providers() == ()


def test_env_providers_detects_present_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    labels = oc._env_providers()
    assert "OpenAI" in labels and "Anthropic" in labels


def test_summary_ready_without_a_provider_for_free_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OpenCode's built-in free models need no sign-in, so an install alone is ready."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    summary = oc.opencode_auth_summary()
    assert summary.ready is True
    assert summary.has_provider is False
    assert summary.describe() == "free models only (no provider signed in)"
    # An env key adds a provider on top.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    summary = oc.opencode_auth_summary()
    assert summary.ready is True
    assert summary.has_provider is True
    assert "env: OpenAI" in summary.describe()


def test_summary_not_ready_when_cli_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert oc.opencode_auth_summary().ready is False  # provider present but no binary


def test_describe_lists_stored_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    text = oc.opencode_auth_summary().describe()
    assert "1 stored (anthropic)" in text
    assert "env: OpenAI" in text


def test_reachable_provider_ids_merges_stored_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    ids = oc.reachable_provider_ids()
    assert "anthropic" in ids  # from auth.json
    assert "openai" in ids  # from env key
    assert "groq" not in ids


def test_reachable_provider_ids_always_include_free_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``opencode`` provider serves free models without any credential."""
    for _provider_id, _label, var in oc._ENV_PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
    assert oc.reachable_provider_ids() == frozenset({"opencode"})


def _write_db(rows: list[tuple[str | None, dict[str, object], int | None, int]]) -> Path:
    """Create a v2 ``opencode.db`` with a ``credential`` table (live 2.0.18 columns)."""
    path = oc.opencode_data_dir() / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT NOT NULL,"
        " value TEXT NOT NULL, connector_id TEXT, method_id TEXT, active INTEGER,"
        " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)"
    )
    for index, (integration_id, value, active, updated) in enumerate(rows):
        conn.execute(
            "INSERT INTO credential VALUES (?, ?, 'x', ?, NULL, NULL, ?, 0, ?)",
            (f"cred_{index}", integration_id, json.dumps(value), active, updated),
        )
    conn.commit()
    conn.close()
    return path


def test_stored_v2_credentials_map_to_legacy_entries(tmp_path: Path) -> None:
    _write_db(
        [
            ("anthropic", {"type": "key", "key": "old"}, None, 1),
            ("anthropic", {"type": "key", "key": "new", "metadata": {"a": "b", "n": 1}}, 1, 0),
            (
                "openai",
                {
                    "type": "oauth",
                    "methodID": "chatgpt-browser",
                    "refresh": "r",
                    "access": "a",
                    "expires": 99,
                    "metadata": {"accountID": "acct"},
                },
                None,
                2,
            ),
            (None, {"type": "key", "key": "orphan"}, None, 3),
            ("broken", {"type": "mystery"}, None, 4),
        ]
    )
    assert oc.stored_v2_credentials() == {
        # The active row wins over a newer inactive one.
        "anthropic": {"type": "api", "key": "new", "metadata": {"a": "b"}},
        "openai": {
            "type": "oauth",
            "refresh": "r",
            "access": "a",
            "expires": 99,
            "accountId": "acct",
        },
    }


def test_stored_v2_credentials_empty_without_db_or_table(tmp_path: Path) -> None:
    assert oc.stored_v2_credentials() == {}
    path = oc.opencode_data_dir() / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()  # a v1 DB without the credential table
    assert oc.stored_v2_credentials() == {}


def test_opencode_db_path_honors_opencode_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert oc.opencode_db_path() == tmp_path / "share" / "opencode" / "opencode.db"
    monkeypatch.setenv("OPENCODE_DB", "custom.db")
    assert oc.opencode_db_path() == tmp_path / "share" / "opencode" / "custom.db"
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "abs.db"))
    assert oc.opencode_db_path() == tmp_path / "abs.db"
    monkeypatch.setenv("OPENCODE_DB", ":memory:")
    assert oc.opencode_db_path() is None


def test_summary_ready_on_free_models_with_empty_v2_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty credential table has no provider but still runs the free models."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_db([])
    summary = oc.opencode_auth_summary()
    assert summary.stored_providers == ()
    assert summary.has_provider is False
    assert summary.ready is True


def test_summary_ready_with_only_v2_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A v2-only login (SQLite, no auth.json) makes the harness ready."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_db([("anthropic", {"type": "key", "key": "k"}, 1, 0)])
    summary = oc.opencode_auth_summary()
    assert summary.stored_providers == ("anthropic",)
    assert summary.ready is True
