"""Tests for the stale-``omnigent-client`` skew guard
(``omnigent.client_compat``) and its wiring into ``omnigent.cli.main``.

Covers: structural detection of both import-error shapes (incl. dotted
submodule and cause/context chains), the version-mismatch gate that keeps a
same-version genuine bug on the crash path, the message shape, and the
``main()`` handoff (skew → hint + exit, no crash report; unrelated / genuine
bug → ``handle_crash``).
"""

from __future__ import annotations

import pytest

from omnigent import client_compat
from omnigent.version import VERSION

_OTHER_VERSION = "0.0.0-stale"
assert _OTHER_VERSION != VERSION


def _cannot_import_name() -> ImportError:
    """The "cannot import name X from 'omnigent_client'" shape."""
    try:
        exec("from omnigent_client import __definitely_missing_symbol__")
    except ImportError as e:
        return e
    raise AssertionError("expected ImportError")


def _module_not_found(name: str = "omnigent_client") -> ModuleNotFoundError:
    """A ``ModuleNotFoundError`` with ``name`` set as CPython would."""
    exc = ModuleNotFoundError(f"No module named {name!r}")
    exc.name = name
    return exc


# --------------------------------------------------------------------------- #
# Structural detection (version-independent)
# --------------------------------------------------------------------------- #
def test_structural_detects_cannot_import_name() -> None:
    assert client_compat._failed_client_import(_cannot_import_name()) is True


def test_structural_detects_module_not_found() -> None:
    assert client_compat._failed_client_import(_module_not_found()) is True


def test_structural_detects_dotted_submodule() -> None:
    assert client_compat._failed_client_import(_module_not_found("omnigent_client.foo")) is True


def test_structural_ignores_unrelated_import_error() -> None:
    assert client_compat._failed_client_import(_module_not_found("some.other.pkg")) is False


def test_structural_ignores_non_import_error() -> None:
    assert client_compat._failed_client_import(ValueError("boom")) is False


def test_structural_detects_in_cause_chain() -> None:
    wrapper = RuntimeError("auth failed")
    wrapper.__cause__ = _cannot_import_name()
    assert client_compat._failed_client_import(wrapper) is True


def test_structural_chain_walk_terminates_on_cycle() -> None:
    exc = ValueError("loop")
    exc.__context__ = exc
    assert client_compat._failed_client_import(exc) is False


# --------------------------------------------------------------------------- #
# Version-mismatch gate
# --------------------------------------------------------------------------- #
def test_skew_true_when_versions_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: _OTHER_VERSION)
    assert client_compat.is_client_skew_error(_cannot_import_name()) is True


def test_skew_false_when_versions_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine missing-symbol bug at the *same* version is not a skew — it
    # must stay on the crash path so the traceback is not hidden.
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: VERSION)
    assert client_compat.is_client_skew_error(_cannot_import_name()) is False
    assert client_compat.client_skew_message(_cannot_import_name()) is None


def test_skew_true_when_client_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: None)
    assert client_compat.is_client_skew_error(_module_not_found()) is True


def test_skew_false_for_unrelated_error() -> None:
    assert client_compat.is_client_skew_error(ValueError("boom")) is False
    assert client_compat.client_skew_message(ValueError("boom")) is None


# --------------------------------------------------------------------------- #
# Message shape
# --------------------------------------------------------------------------- #
def test_message_names_versions_and_both_fixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: _OTHER_VERSION)
    msg = client_compat.client_skew_message(_cannot_import_name())
    assert msg is not None
    assert _OTHER_VERSION in msg  # installed client version named
    assert VERSION in msg  # running omnigent version named
    assert "omni upgrade" in msg  # installed-wheel fix
    assert "uv sync" in msg  # dev-clone fix
    assert "__definitely_missing_symbol__" in msg  # underlying error preserved


def test_message_when_client_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: None)
    msg = client_compat.client_skew_message(_module_not_found())
    assert msg is not None
    assert "not installed" in msg
    assert VERSION in msg


# --------------------------------------------------------------------------- #
# main() handoff
# --------------------------------------------------------------------------- #
@pytest.fixture
def stubbed_main(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Neutralise ``main()``'s startup side effects and record whether the
    friendly crash handler runs. Returns a dict the test reads afterwards.

    Yields control to the test to set the exception ``cli()`` raises via
    ``state["raise"]``.
    """
    from omnigent import cli as cli_mod
    from omnigent import cli_diagnostics, crash_handler

    state: dict = {"raise": None, "handle_crash_called": False}

    monkeypatch.setattr(crash_handler, "install_crash_handler", lambda **_: None)
    monkeypatch.setattr(
        crash_handler, "handle_crash", lambda exc: state.__setitem__("handle_crash_called", True)
    )
    monkeypatch.setattr(cli_mod, "_migrate_legacy_state_dir", lambda: None)
    monkeypatch.setattr(cli_mod, "_maybe_fast_backfill_install_ledger", lambda argv: None)
    monkeypatch.setattr(cli_mod, "_should_skip_update_check", lambda argv: True)
    monkeypatch.setattr(cli_diagnostics, "setup_cli_logging", lambda argv: None)

    def _cli(*_a, **_k):
        raise state["raise"]

    monkeypatch.setattr(cli_mod, "cli", _cli)
    monkeypatch.setattr("sys.argv", ["omnigent", "run"])
    return state


def test_main_prints_hint_and_skips_crash_on_skew(
    stubbed_main: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from omnigent import cli as cli_mod

    monkeypatch.setattr(client_compat, "installed_client_version", lambda: _OTHER_VERSION)
    stubbed_main["raise"] = _cannot_import_name()

    with pytest.raises(SystemExit) as ei:
        cli_mod.main()

    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "does not match omnigent" in err
    assert "omni upgrade" in err
    assert stubbed_main["handle_crash_called"] is False  # no crash report


def test_main_uses_crash_handler_for_same_version_bug(
    stubbed_main: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from omnigent import cli as cli_mod

    # Same version → genuine bug, not skew: the crash handler must run.
    monkeypatch.setattr(client_compat, "installed_client_version", lambda: VERSION)
    stubbed_main["raise"] = _cannot_import_name()

    with pytest.raises(SystemExit) as ei:
        cli_mod.main()

    assert ei.value.code == 1
    assert stubbed_main["handle_crash_called"] is True
    assert "does not match omnigent" not in capsys.readouterr().err


def test_main_uses_crash_handler_for_unrelated_error(
    stubbed_main: dict, capsys: pytest.CaptureFixture
) -> None:
    from omnigent import cli as cli_mod

    stubbed_main["raise"] = ValueError("something else broke")

    with pytest.raises(SystemExit) as ei:
        cli_mod.main()

    assert ei.value.code == 1
    assert stubbed_main["handle_crash_called"] is True
