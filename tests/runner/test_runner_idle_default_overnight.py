"""Runner idle-timeout default must let a session survive overnight.

The runner process runs an inactivity watchdog that shuts the runner
down after ``runner.idle_timeout_s`` seconds without activity. When the
global config file does not set that key, the runner falls back to a
built-in default. A default shorter than an overnight gap means a
session left idle in the evening loses its runner, and the next-day
visit finds a dead session that needs an awkward respawn — the exact
next-day-experience failure this test guards against.

These tests pin the product expectation that the built-in default is at
least 24 hours, while an explicit ``runner.idle_timeout_s`` keeps
winning over the default.

Usage::

    python -m pytest tests/runner/test_runner_idle_default_overnight.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner._entry import _load_runner_idle_timeout_s_from_config

# An idle window must span a full overnight gap: evening to next morning.
_ONE_DAY_S = 24 * 60 * 60


def test_default_runner_idle_timeout_survives_overnight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no config file, the runner idle default is at least 24 hours.

    This is the value the real runner resolves at boot when the user never
    touched ``~/.omnigent/config.yaml`` — the overwhelmingly common case.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home with no config file.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    timeout_s = _load_runner_idle_timeout_s_from_config()

    assert timeout_s >= _ONE_DAY_S, (
        f"runner idle-timeout default is {timeout_s:.0f}s "
        f"({timeout_s / 3600:.1f}h); a session left idle overnight loses its "
        f"runner before the next-day visit (expected >= {_ONE_DAY_S}s / 24h)"
    )


def test_default_runner_idle_timeout_survives_overnight_with_empty_runner_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config file without ``runner.idle_timeout_s`` gets the same default.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("runner: {}\n", encoding="utf-8")

    timeout_s = _load_runner_idle_timeout_s_from_config()

    assert timeout_s >= _ONE_DAY_S, (
        f"runner idle-timeout default is {timeout_s:.0f}s "
        f"({timeout_s / 3600:.1f}h); a session left idle overnight loses its "
        f"runner before the next-day visit (expected >= {_ONE_DAY_S}s / 24h)"
    )


def test_explicit_idle_timeout_still_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit ``runner.idle_timeout_s`` overrides the built-in default.

    Guards the fix from hardcoding the new default: a user who tuned the
    idle window must keep their value.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Isolated config home.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "runner:\n  idle_timeout_s: 900\n",
        encoding="utf-8",
    )

    assert _load_runner_idle_timeout_s_from_config() == 900.0
