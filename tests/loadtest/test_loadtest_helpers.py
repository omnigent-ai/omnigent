"""Unit tests for the Omnigent load test's pure helpers.

Deterministic, no stack boot / no network — the fast regression net for the
``dev/loadtest/`` tooling. A full boot-a-stack e2e (server + mock LLM + real
host + runner subprocesses) is far too heavy and flaky for CI for a dev-only
tool; instead these cover the logic most likely to silently break under
maintenance: locust argv wiring, the mock-reply builder, and result formatting
(including the Aggregated-row dedupe). Mirrors
``tests/benchmarks/test_benchmark_smoke.py``, which unit-tests its pure layers
the same way.

``run.py`` is loaded by path (it's a script, not an importable package module);
its top-level imports are stdlib + ``dev.benchmarks`` is only imported lazily
inside ``_boot_and_run``, so importing the module for these helper tests is
cheap and dependency-free.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_RUN_PY = Path(__file__).resolve().parents[2] / "dev" / "loadtest" / "run.py"


def _load_run_module():
    """Import dev/loadtest/run.py as a module (top level is import-cheap)."""
    spec = importlib.util.spec_from_file_location("_loadtest_run", _RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run_module()


def _args(**over: object) -> argparse.Namespace:
    """Build a parsed-args namespace with all run.py fields, overridable."""
    base = {
        "users": 4,
        "spawn_rate": 1.0,
        "run_time": "120s",
        "sessions_per_user": 2,
        "turns_per_session": 4,
        "reply_words": 60,
        "out_dir": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ── _fmt_num ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14.508", "14.5"),
        ("0", "0.0"),
        ("", "-"),
        ("7.9", "7.9"),  # an Ops/s value — same formatter, not just ms
        ("N/A", "N/A"),  # non-numeric passes through unchanged
    ],
)
def test_fmt_num(raw: str, expected: str) -> None:
    assert run._fmt_num(raw) == expected


# ── _mock_reply ───────────────────────────────────────────────────────


def test_mock_reply_word_count_and_shape() -> None:
    reply = run._mock_reply(60)
    # ~60 words (helper pads to at least the count), ends with a period.
    assert len(reply.split()) >= 60
    assert reply.endswith(".")
    # Small counts are honored too.
    assert len(run._mock_reply(5).split()) >= 5


# ── _build_locust_argv (uses the current interpreter, headless, CSV/HTML) ─


def test_build_locust_argv(tmp_path: Path) -> None:
    import sys

    argv = run._build_locust_argv(
        _args(users=7, spawn_rate=2.0, run_time="45s"), "http://localhost:9999", tmp_path
    )
    # Must launch `<python> -m locust`, never a bare `locust` off PATH.
    assert argv[:3] == [sys.executable, "-m", "locust"]
    assert argv[argv.index("--host") + 1] == "http://localhost:9999"
    assert argv[argv.index("-u") + 1] == "7"
    assert argv[argv.index("-r") + 1] == "2.0"
    assert argv[argv.index("-t") + 1] == "45s"
    assert "--headless" in argv and "--csv" in argv and "--html" in argv


# ── _read_stats + _write_summary (result formatting) ──────────────────


def test_read_stats_missing_file_is_empty(tmp_path: Path) -> None:
    assert run._read_stats(tmp_path / "nope.csv") == []


def _stats_row(name: str, *, count: int, fails: int) -> dict[str, str]:
    """Build a minimal locust ``_stats.csv`` row dict for summary tests."""
    return {
        "Name": name,
        "Request Count": str(count),
        "Failure Count": str(fails),
        "Average Response Time": "14.5",
        "Median Response Time": "11.0",
        "95%": "21.0",
        "99%": "23.0",
        "Max Response Time": "40.0",
        "Requests/s": "1.3",
    }


def test_write_summary_dedupes_aggregated_row(tmp_path: Path) -> None:
    # Two per-op rows + locust's Aggregated row (already their sum). The headline
    # must count only the per-op rows (30 + 20 = 50), NOT also Aggregated —
    # otherwise the total double-counts.
    rows = [
        _stats_row("turn", count=30, fails=1),
        _stats_row("session create", count=20, fails=1),
        _stats_row("Aggregated", count=50, fails=2),
    ]
    run._write_summary(tmp_path, _args(), rows, exit_code=1)
    text = (tmp_path / "summary.md").read_text()
    assert "50 operations, 2 failures" in text  # 50, not 100
    assert "FAIL" in text  # exit_code != 0 → FAIL outcome
    assert "| turn |" in text and "| session create |" in text


def test_write_summary_pass_outcome(tmp_path: Path) -> None:
    run._write_summary(tmp_path, _args(), [_stats_row("turn", count=10, fails=0)], exit_code=0)
    assert "PASS" in (tmp_path / "summary.md").read_text()


def test_write_run_config_records_inputs(tmp_path: Path) -> None:
    import json

    run._write_run_config(tmp_path, _args(users=5), ["locust", "-u", "5"], exit_code=0)
    cfg = json.loads((tmp_path / "run_config.json").read_text())
    assert cfg["scenario"] == "omnigent_load_test"
    assert cfg["users_hosts"] == 5
    assert cfg["exit_code"] == 0
    assert cfg["model"] == "mock (zero-latency)"
