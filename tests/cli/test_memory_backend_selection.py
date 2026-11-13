"""``omnigent server`` picks the memory backend from the flag or the config file."""

from __future__ import annotations

import click
import pytest

from omnigent.cli import _create_memory_store
from omnigent.stores.memory_store.hindsight_store import HindsightMemoryStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore


def test_default_is_sqlalchemy_on_the_server_database(db_uri: str) -> None:
    assert isinstance(_create_memory_store(None, db_uri, None), SqlAlchemyMemoryStore)


def test_config_file_can_select_hindsight_and_the_flag_overrides_it(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[str | None, str]] = []
    monkeypatch.setenv("HINDSIGHT_API_KEY", "key-from-env")
    monkeypatch.setattr(
        "omnigent.stores.memory_store.hindsight_store.create_hindsight_client",
        lambda api_url, api_key: created.append((api_url, api_key)) or object(),
    )
    config = {
        "backend": "hindsight",
        "hindsight": {"api_url": "https://hs.example.com", "api_key": "env:HINDSIGHT_API_KEY"},
    }

    store = _create_memory_store(None, db_uri, config)
    overridden = _create_memory_store("sqlalchemy", db_uri, config)

    assert isinstance(store, HindsightMemoryStore)
    assert created == [("https://hs.example.com", "key-from-env")]
    assert isinstance(overridden, SqlAlchemyMemoryStore)


@pytest.mark.parametrize(
    "config",
    [
        {"backend": "hindsight"},
        {"backend": "hindsight", "hindsight": {}},
        {"backend": "hindsight", "hindsight": {"api_key": ""}},
    ],
)
def test_hindsight_without_a_key_reference_fails_with_guidance(db_uri: str, config: dict) -> None:
    with pytest.raises(click.ClickException, match=r"memory\.hindsight\.api_key"):
        _create_memory_store(None, db_uri, config)


def test_an_unset_key_variable_is_a_startup_error_not_a_silent_fallback(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    config = {"backend": "hindsight", "hindsight": {"api_key": "env:HINDSIGHT_API_KEY"}}

    with pytest.raises(Exception, match="HINDSIGHT_API_KEY"):
        _create_memory_store(None, db_uri, config)


def test_unknown_backend_is_rejected(db_uri: str) -> None:
    with pytest.raises(click.ClickException, match="Unknown memory backend"):
        _create_memory_store("redis", db_uri, None)
