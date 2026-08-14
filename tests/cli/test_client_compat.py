"""Tests for the stale-``omnigent-client`` skew guard
(``omnigent.client_compat``).

Covers detection of both skew import-error shapes, rejection of unrelated
errors, cause/context chain walking, and the shape of the actionable
message (version + update commands + underlying error).
"""

from __future__ import annotations

import pytest

from omnigent import client_compat
from omnigent.version import VERSION


def _cannot_import_name() -> ImportError:
    """The "cannot import name X from 'omnigent_client'" shape."""
    try:
        # omnigent_client exists but (pretend) lacks this symbol.
        exec("from omnigent_client import __definitely_missing_symbol__")
    except ImportError as e:  # pragma: no branch
        return e
    raise AssertionError("expected ImportError")


def _module_not_found() -> ModuleNotFoundError:
    """The "No module named 'omnigent_client'" shape."""
    try:
        __import__("omnigent_client.__nope__")
    except ModuleNotFoundError as e:
        # Re-point name in case the submodule import reports the submodule.
        e.name = "omnigent_client"
        return e
    raise AssertionError("expected ModuleNotFoundError")


def test_detects_cannot_import_name() -> None:
    assert client_compat.is_client_skew_error(_cannot_import_name()) is True


def test_detects_module_not_found() -> None:
    assert client_compat.is_client_skew_error(_module_not_found()) is True


def test_ignores_unrelated_import_error() -> None:
    exc = ImportError("cannot import name 'x' from 'some.other.pkg'")
    exc.name = "some.other.pkg"
    assert client_compat.is_client_skew_error(exc) is False
    assert client_compat.client_skew_message(exc) is None


def test_ignores_non_import_error() -> None:
    assert client_compat.is_client_skew_error(ValueError("boom")) is False


def test_detects_skew_in_cause_chain() -> None:
    skew = _cannot_import_name()
    wrapper = RuntimeError("auth failed")
    wrapper.__cause__ = skew
    assert client_compat.is_client_skew_error(wrapper) is True


def test_chain_walk_terminates_on_cycle() -> None:
    # A self-referential context must not loop forever.
    exc = ValueError("loop")
    exc.__context__ = exc
    assert client_compat.is_client_skew_error(exc) is False


def test_message_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: "0.9.0.dev1")
    msg = client_compat.client_skew_message(_cannot_import_name())
    assert msg is not None
    assert "0.9.0.dev1" in msg  # stale client version named
    assert VERSION in msg  # running omnigent version named
    assert "omni upgrade" in msg  # installed-wheel fix
    assert "uv sync" in msg  # dev-clone fix
    assert "__definitely_missing_symbol__" in msg  # underlying error preserved


def test_message_when_client_version_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: None)
    msg = client_compat.client_skew_message(_cannot_import_name())
    assert msg is not None
    assert "not installed" in msg
    assert VERSION in msg
