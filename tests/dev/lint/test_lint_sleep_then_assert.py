"""Tests for ``dev/lint/lint_sleep_then_assert.py``."""

from __future__ import annotations

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
    await asyncio.sleep(0)
    assert task.done()
    time.sleep(0.5)  # custom-lint: disable=sleep-then-assert -- simulates a slow peer
    assert task.done()
    time.sleep(0.4)
    worker.step()
    worker.step()
    worker.step()
    assert worker.done
"""


def test_flags_fixed_sleeps_that_an_instant_presence_assert_relies_on() -> None:
    assert lint.sleep_then_assert_lines(_SOURCE) == [6, 11]


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
