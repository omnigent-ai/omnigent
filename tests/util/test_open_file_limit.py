"""Raising the process's soft open-file limit at startup."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Iterable

import pytest

from omnigent.util.open_file_limit import (
    _FALLBACK_SOFT_LIMITS,
    DEFAULT_SOFT_OPEN_FILE_LIMIT,
    OpenFileLimit,
    raise_soft_open_file_limit,
)

# Linux's ``resource.RLIM_INFINITY``; macOS exposes a large positive sentinel instead.
_INFINITY = -1
_LOGGER_NAME = "omnigent.util.open_file_limit"


class _FakeResource:
    """Stand-in for ``resource`` that records ``setrlimit`` calls and rejects chosen values."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = _INFINITY

    def __init__(
        self,
        soft: int,
        hard: int,
        *,
        reject: Iterable[int] = (),
        reject_with: type[Exception] = ValueError,
    ) -> None:
        self.limits = (soft, hard)
        self.reject = set(reject)
        self.reject_with = reject_with
        self.calls: list[tuple[int, int]] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        assert which == self.RLIMIT_NOFILE
        self.calls.append(limits)
        if limits[0] in self.reject:
            raise self.reject_with("current limit exceeds maximum limit")
        self.limits = limits


@pytest.fixture
def fake_resource(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeResource]:
    """Install a fake ``resource`` module the helper imports lazily."""

    def install(
        soft: int,
        hard: int,
        *,
        reject: Iterable[int] = (),
        reject_with: type[Exception] = ValueError,
    ) -> _FakeResource:
        fake = _FakeResource(soft, hard, reject=reject, reject_with=reject_with)
        monkeypatch.setitem(sys.modules, "resource", fake)
        return fake

    return install


def test_raises_soft_limit_to_the_target_under_a_finite_hard_limit(
    fake_resource: Callable[..., _FakeResource], caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_resource(256, 65536)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = raise_soft_open_file_limit()

    assert result == OpenFileLimit(65536, 65536)
    assert fake.calls == [(65536, 65536)]
    assert "raised soft open-file limit from 256 to 65536 (hard 65536)" in caplog.text


def test_finite_hard_limit_caps_the_target(fake_resource: Callable[..., _FakeResource]) -> None:
    fake = fake_resource(256, 4096)

    assert raise_soft_open_file_limit() == OpenFileLimit(4096, 4096)
    assert fake.calls == [(4096, 4096)]


def test_unlimited_hard_limit_falls_back_to_macos_open_max(
    fake_resource: Callable[..., _FakeResource], caplog: pytest.LogCaptureFixture
) -> None:
    """macOS rejects a soft limit above kern.maxfilesperproc even with an unlimited hard limit."""
    fake = fake_resource(256, _INFINITY, reject={DEFAULT_SOFT_OPEN_FILE_LIMIT})

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = raise_soft_open_file_limit()

    assert result == OpenFileLimit(10240, _INFINITY)
    assert fake.calls == [(65536, _INFINITY), (10240, _INFINITY)]
    assert "raised soft open-file limit from 256 to 10240 (hard unlimited)" in caplog.text


@pytest.mark.parametrize("soft", [DEFAULT_SOFT_OPEN_FILE_LIMIT, 70000, _INFINITY])
def test_sufficient_soft_limit_is_left_alone(
    fake_resource: Callable[..., _FakeResource], soft: int
) -> None:
    fake = fake_resource(soft, _INFINITY)

    assert raise_soft_open_file_limit() == OpenFileLimit(soft, _INFINITY)
    assert fake.calls == []


def test_tuned_kernel_below_open_max_settles_on_a_smaller_step(
    fake_resource: Callable[..., _FakeResource],
) -> None:
    """A host with kern.maxfilesperproc below OPEN_MAX still gets headroom."""
    fake = fake_resource(256, _INFINITY, reject={65536, 10240})

    assert raise_soft_open_file_limit() == OpenFileLimit(4096, _INFINITY)
    assert fake.calls == [(65536, _INFINITY), (10240, _INFINITY), (4096, _INFINITY)]


@pytest.mark.parametrize("reject_with", [ValueError, OSError])
@pytest.mark.parametrize(
    ("soft", "rejected", "attempts", "level"),
    [
        pytest.param(
            256,
            {65536, 10240, 4096, 1024},
            [65536, 10240, 4096, 1024],
            logging.WARNING,
            id="inherited-256-every-step-rejected-warns",
        ),
        pytest.param(
            12000,
            {65536},
            [65536],
            logging.INFO,
            id="already-above-open-max-is-informational",
        ),
    ],
)
def test_rejected_raise_keeps_the_inherited_limit_and_logs_by_headroom(
    fake_resource: Callable[..., _FakeResource],
    caplog: pytest.LogCaptureFixture,
    reject_with: type[Exception],
    soft: int,
    rejected: set[int],
    attempts: list[int],
    level: int,
) -> None:
    """A rejected raise keeps the inherited limit; it warns only when headroom is tight."""
    fake = fake_resource(soft, _INFINITY, reject=rejected, reject_with=reject_with)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = raise_soft_open_file_limit()

    assert result == OpenFileLimit(soft, _INFINITY)
    assert [wanted for wanted, _ in fake.calls] == attempts
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert [r.levelno for r in records] == [level]
    assert (
        f"could not raise soft open-file limit from {soft} (hard unlimited)"
        in records[0].getMessage()
    )


def test_platform_without_rlimits_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no ``resource`` module; startup must not depend on it."""
    monkeypatch.setitem(sys.modules, "resource", None)

    assert raise_soft_open_file_limit() is None


def test_real_process_raises_its_own_soft_limit() -> None:
    """Against the live kernel: a process inheriting a soft limit of 256 raises it at startup.

    Runs in a child so the rlimit mutation never touches the test runner.
    """
    pytest.importorskip("resource")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, resource\n"
            "from omnigent.util.open_file_limit import raise_soft_open_file_limit\n"
            "_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
            "inherited = 256 if _soft == resource.RLIM_INFINITY else min(_soft, 256)\n"
            "if hard != resource.RLIM_INFINITY:\n"
            "    inherited = min(inherited, hard)\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE, (inherited, hard))\n"
            "raised = raise_soft_open_file_limit()\n"
            "print(json.dumps([list(raised), list(resource.getrlimit(resource.RLIMIT_NOFILE)),"
            " hard == resource.RLIM_INFINITY]))\n",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    raised, live, hard_unlimited = json.loads(child.stdout)
    soft, hard = live
    if not hard_unlimited and hard <= 256:
        pytest.skip(f"hard RLIMIT_NOFILE ({hard}) is too low to raise the soft limit above 256")
    # macOS may reject 65536 under an unlimited hard limit and settle on a fallback.
    accepted = (
        {DEFAULT_SOFT_OPEN_FILE_LIMIT, *_FALLBACK_SOFT_LIMITS}
        if hard_unlimited
        else {min(hard, DEFAULT_SOFT_OPEN_FILE_LIMIT)}
    )
    assert raised == live
    if hard_unlimited and soft == 256:
        pytest.skip(
            "the kernel rejected every soft-limit candidate; inherited limit kept by design"
        )
    assert soft in accepted and soft > 256
