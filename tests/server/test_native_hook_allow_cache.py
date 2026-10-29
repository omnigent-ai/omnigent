"""Server side of the native hook allow cache: the window it grants and its config."""

from __future__ import annotations

import math

import click
import pytest

from omnigent.cli import _parse_native_hook_allow_cache_seconds
from omnigent.runtime.caps import RuntimeCaps
from omnigent.runtime.policies.builder import (
    _DEFAULT_POLICY_SPECS_CACHE,
    _SESSION_POLICY_SPECS_CACHE,
    _load_default_policy_specs,
    _load_session_policy_specs,
)
from omnigent.server.routes.sessions.routes_hooks import _no_policy_allow_body

_SESSION_ID = "4f5c2c3a1b6d4e0f9a8b7c6d5e4f3a2b"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (60.0, 60.0),
        (86_400.0, 300.0),
        (0.0, None),
        (-5.0, None),
        (math.inf, None),
        (math.nan, None),
    ],
    ids=["window", "capped", "zero", "negative", "infinite", "nan"],
)
def test_no_policy_allow_body_window(configured: float, expected: float | None) -> None:
    """Zero or negative turns the cache off, a non-finite value never means
    forever, and a mistyped window cannot leave policy changes unenforced for
    hours."""
    caps = RuntimeCaps(native_hook_allow_cache_seconds=lambda: configured)

    body = _no_policy_allow_body(caps, _SESSION_ID, None)

    assert body.get("allow_cache_seconds") == expected
    assert body["result"] == "POLICY_ACTION_ALLOW"


def test_failing_window_resolver_still_allows() -> None:
    """A broken resolver drops the window but never fails the hook."""

    def broken() -> float:
        raise RuntimeError("flag service down")

    body = _no_policy_allow_body(
        RuntimeCaps(native_hook_allow_cache_seconds=broken), _SESSION_ID, None
    )

    assert body == {"result": "POLICY_ACTION_ALLOW"}


def test_cache_window_counts_from_the_policy_list_reads(db_uri: str) -> None:
    """The window shrinks by the age of the cached lists behind the no-policy answer.

    Otherwise a list cached just before an admin added a policy elsewhere
    would grant a full fresh window on top of its own cache lifetime.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    from omnigent.db.db_models import current_workspace_id
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    store = SqlAlchemyPolicyStore(db_uri)
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    _SESSION_POLICY_SPECS_CACHE.clear()
    assert _load_default_policy_specs(store) == []
    assert _load_session_policy_specs(_SESSION_ID, store) == []
    workspace_id = current_workspace_id()
    specs, loaded_at = _DEFAULT_POLICY_SPECS_CACHE[workspace_id]
    _DEFAULT_POLICY_SPECS_CACHE[workspace_id] = (specs, loaded_at - 25.0)
    caps = RuntimeCaps(native_hook_allow_cache_seconds=lambda: 60.0)

    body = _no_policy_allow_body(caps, _SESSION_ID, store)

    assert 34.0 < body["allow_cache_seconds"] <= 35.0
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    _SESSION_POLICY_SPECS_CACHE.clear()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (60, 60.0), (2.5, 2.5)],
    ids=["absent", "int", "float"],
)
def test_parse_native_hook_allow_cache_seconds(raw: object, expected: float | None) -> None:
    resolver = _parse_native_hook_allow_cache_seconds(raw)

    assert (resolver() if resolver is not None else None) == expected


@pytest.mark.parametrize("raw", [True, "60", [60]], ids=["bool", "string", "list"])
def test_parse_native_hook_allow_cache_seconds_rejects_non_numbers(raw: object) -> None:
    """A mistyped value fails at startup instead of on every evaluate request."""
    with pytest.raises(click.ClickException):
        _parse_native_hook_allow_cache_seconds(raw)
