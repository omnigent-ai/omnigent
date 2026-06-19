from __future__ import annotations

from pathlib import Path
from typing import Any

import tests.conftest as root_conftest


class _Config:
    rootpath = Path("/repo")

    def __init__(
        self,
        *,
        args: list[str],
        ignore: list[str] | None = None,
        ignore_glob: list[str] | None = None,
        keyword: str = "",
        markexpr: str = "",
        deselect: list[str] | None = None,
        testpaths: list[str] | None = None,
    ) -> None:
        self.args = args
        self._ignore = ignore or []
        self._ignore_glob = ignore_glob or []
        self._keyword = keyword
        self._markexpr = markexpr
        self._deselect = deselect or []
        self._testpaths = testpaths or ["tests"]

    def getoption(self, name: str, default: Any = None) -> Any:
        if name == "ignore":
            return self._ignore
        if name == "ignore_glob":
            return self._ignore_glob
        if name == "keyword":
            return self._keyword
        if name == "markexpr":
            return self._markexpr
        if name == "deselect":
            return self._deselect
        return default

    def getini(self, name: str) -> Any:
        if name == "testpaths":
            return self._testpaths
        raise AssertionError(f"unexpected ini option: {name}")


def test_stale_known_failures_ignore_entries_outside_collection_scope(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        root_conftest,
        "_KNOWN_FAILURES",
        {
            "tests/e2e/test_flow.py::test_flaky": {"mode": "skip"},
            "tests/inner/egress/test_rules.py::test_missing": {"mode": "skip"},
            "tests/spec/test_validator.py::test_seen": {"mode": "skip"},
        },
    )
    config = _Config(
        args=[
            "tests/inner/egress/test_rules.py",
            "tests/spec/test_validator.py::test_seen",
        ],
        ignore=["tests/e2e"],
    )

    stale = root_conftest._stale_known_failure_ids(
        config,  # type: ignore[arg-type]
        {"tests/spec/test_validator.py::test_seen"},
    )

    assert stale == ["tests/inner/egress/test_rules.py::test_missing"]


def test_stale_known_failures_ignore_default_ignored_testpaths(monkeypatch) -> None:
    monkeypatch.setattr(
        root_conftest,
        "_KNOWN_FAILURES",
        {
            "tests/e2e/test_flow.py::test_flaky": {"mode": "skip"},
            "tests/integration/test_api.py::test_flaky": {"mode": "skip"},
            "tests/spec/test_validator.py::test_missing": {"mode": "skip"},
        },
    )
    config = _Config(
        args=[],
        ignore=["tests/e2e", "/repo/tests/integration"],
        testpaths=["tests"],
    )

    stale = root_conftest._stale_known_failure_ids(config, set())  # type: ignore[arg-type]

    assert stale == ["tests/spec/test_validator.py::test_missing"]


def test_stale_known_failures_warn_for_selected_nodeid(monkeypatch) -> None:
    monkeypatch.setattr(
        root_conftest,
        "_KNOWN_FAILURES",
        {
            "tests/spec/test_validator.py::test_missing": {"mode": "skip"},
        },
    )
    config = _Config(
        args=["tests/spec/test_validator.py::test_missing"],
    )

    stale = root_conftest._stale_known_failure_ids(config, set())  # type: ignore[arg-type]

    assert stale == ["tests/spec/test_validator.py::test_missing"]


def test_stale_known_failures_skip_warning_for_explicit_test_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        root_conftest,
        "_KNOWN_FAILURES",
        {
            "tests/spec/test_validator.py::test_missing": {"mode": "skip"},
        },
    )

    for config in (
        _Config(args=["tests/spec"], keyword="not test_missing"),
        _Config(args=["tests/spec"], markexpr="not live"),
        _Config(args=["tests/spec"], deselect=["tests/spec/test_validator.py::test_missing"]),
    ):
        stale = root_conftest._stale_known_failure_ids(
            config,  # type: ignore[arg-type]
            set(),
        )

        assert stale == []
