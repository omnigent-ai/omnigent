"""Tests for ``dev/lint/lint_sleep_then_assert.py``."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import dev.lint.lint_sleep_then_assert as lint

_SOURCE = """\
import asyncio, time


def test_sync(worker):
    worker.start()
    time.sleep(0.1)
    assert worker.done


async def test_async(page, mock, task, flag):
    await asyncio.sleep(0.2)
    mock.assert_called_once()
    while not flag.is_set():
        await asyncio.sleep(0.01)
    assert flag.is_set()
    await page.wait_for_timeout(500)
    expect(page.get_by_text("ready")).to_be_visible()
    await asyncio.sleep(0.3)
    assert not task.done()
    time.sleep(0.3)
    assert task.result is None
    time.sleep(0.3)
    mock.assert_not_called()
    time.sleep(0.5)  # custom-lint: disable=sleep-then-assert -- simulates a slow peer
    assert task.done()
    await asyncio.sleep(0)
    assert task.done()
    time.sleep(0.4)
    worker.step()
    worker.step()
    worker.step()
    assert worker.done


async def test_keywords_and_mixed_checks(page, task, worker, failed):
    await asyncio.sleep(delay=0.1)
    assert task.done()
    await asyncio.sleep(delay=0)
    assert task.done()
    time.sleep(0.1)
    assert not failed
    assert worker.done
    await page.wait_for_timeout(timeout=200)
    await expect(page.get_by_text("ready")).to_be_visible()
    assert worker.done
    await asyncio.sleep(0.1)
    result = await asyncio.to_thread(worker.result)
    assert result
    time.sleep(1)
    assert not task.done() and failed == []
    time.sleep(1)
    assert not task.done() and worker.done
"""


def test_flags_fixed_sleeps_that_an_instant_presence_assert_relies_on() -> None:
    # Flagged: positional and keyword sleeps, a presence check after an absence check,
    # a check after an unrelated `to_*` call that is not Playwright's expect(), and an
    # `and` mixing absence with presence. An all-absence `and` is not flagged.
    assert lint.sleep_then_assert_lines(_SOURCE) == [6, 11, 36, 40, 46, 51]


def test_baseline_rejects_new_cases_and_asks_to_lower_fixed_ones() -> None:
    found = {"tests/new_case.py": [3], "tests/kept.py": [5, 9], "tests/fixed.py": [2]}
    baseline = {"tests/kept.py": 2, "tests/fixed.py": 3}
    violations = lint.compare_with_baseline(found, baseline)
    assert [message.split(":", 1)[0] for message in violations] == [
        "tests/new_case.py",
        "tests/fixed.py",
    ]
    assert "0 grandfathered" in violations[0]
    assert "lower its count" in violations[1]


def test_precommit_hook_runs_when_only_the_baseline_changes() -> None:
    config = yaml.safe_load(Path(".pre-commit-config.yaml").read_text())
    hook = next(
        hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "custom-lint"
    )
    assert re.search(hook["files"], lint.BASELINE.relative_to(Path.cwd()).as_posix())
