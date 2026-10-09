"""Block meaningful UI regressions without mistaking runner noise for one."""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dev.benchmarks.ui import run as ui_run
from dev.benchmarks.ui.run import (
    assess_reports,
    budget_failures,
    make_report,
    measurement_failures,
    parse_args,
    required_journeys,
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        iterations=20,
        runs=3,
        warmup=3,
        cpu_throttle=4,
        max_key_to_frame_ms=100,
        max_style_layout_ms=16,
        threshold=1.0,
        min_regression_ms=5.0,
    )


def _report() -> dict:
    samples = {
        mode: [
            {
                "session_open": [1000],
                "key_to_frame": [30.0] * 20,
                "style_layout": [2.0] * 20,
                "script_ms": [1.0] * 20,
                "wall_time_s": 1,
                "dom_elements": 3000,
                "dom_elements_after": 3000,
                "page_errors": [],
            }
            for _ in range(3)
        ]
        for mode in ("browser", "mac_css")
    }
    return make_report(samples, _args(), "ui-revision", "test-chromium")


def test_ui_report_preserves_raw_samples_and_revision() -> None:
    report = _report()
    assert report["git_sha"] == "ui-revision"
    assert report["harness"] == "chromium-ui"
    assert report["config"]["browser_version"] == "test-chromium"
    assert report["config"]["cpu_throttle"] == 4
    assert set(report["journeys"]) == set(required_journeys())
    assert report["samples"]["browser"][0]["key_to_frame"] == [30.0] * 20
    assert budget_failures(report, _args()) == []


@pytest.mark.parametrize("mode", ["browser", "mac_css"])
@pytest.mark.parametrize("metric", ["session_open", "key_to_frame", "style_layout"])
@pytest.mark.parametrize("variant", ["baseline", "candidate"])
def test_ui_gate_requires_every_run_and_keystroke(mode: str, metric: str, variant: str) -> None:
    name = f"{mode}_{metric}"
    reports = {"candidate": _report(), "baseline": _report()}
    report = reports[variant]
    report["journeys"][name]["runs"].pop()
    assert assess_reports(reports, _args())[0] == [f"{variant}: {name}: incomplete samples"]

    reports[variant] = _report()
    reports[variant]["journeys"][name]["runs"][1]["n_success"] -= 1
    assert assess_reports(reports, _args())[0] == [f"{variant}: {name}: incomplete samples"]

    del reports[variant]["journeys"][name]
    assert assess_reports(reports, _args())[0] == [f"{variant}: {name}: incomplete samples"]


@pytest.mark.parametrize("bad_value", [math.inf, math.nan, -1.0, None])
@pytest.mark.parametrize("metric", ["p50_ms", "p95_ms"])
def test_ui_gate_rejects_even_one_invalid_run(bad_value: float | None, metric: str) -> None:
    report = _report()
    report["journeys"]["browser_style_layout"]["runs"][1][metric] = bad_value
    assert measurement_failures(report, _args()) == [
        "browser_style_layout: invalid timing samples"
    ]
    for variant in ("baseline", "candidate"):
        reports = {"baseline": _report(), "candidate": _report(), variant: report}
        assert assess_reports(reports, _args())[0]


def test_one_slow_run_does_not_fail_the_ui_budget() -> None:
    report = _report()
    original = copy.deepcopy(report)
    report["journeys"]["browser_style_layout"]["runs"][0]["p95_ms"] = 200
    assert budget_failures(report, _args()) == []
    # The outlier remains visible in the report even though the median gates.
    assert report != original
    assert report["journeys"]["browser_style_layout"]["runs"][0]["p95_ms"] == 200


@pytest.mark.parametrize(
    ("baseline", "candidate", "expected_status"),
    [
        ([150, 150, 150], [150, 150, 150], "ok"),  # Unchanged on a slow machine.
        ([20, 20, 20], [22, 22, 22], "ok"),  # Small relative increase.
        ([2, 2, 2], [7, 7, 7], "ok"),  # At the absolute noise floor.
        ([2, 2, 2], [10, 10, 10], "advisory"),  # Still within the rendering budget.
        ([5, 5, 5], [5, 5, 200], "ok"),  # One isolated slow run.
        ([5, 5, 100], [5, 100, 100], "advisory"),  # Medians differ; only one pair regresses.
        ([5, 5, 5], [150, 150, 150], "regression"),  # Whole-document restyle.
        ([5, 5, 5], [5, 50, 50], "regression"),  # Repeats in two of three pairs.
        ([0, 0, 0], [30, 30, 30], "regression"),  # A real zero baseline is not missing data.
    ],
)
def test_paired_gate_filters_noise(
    baseline: list[float], candidate: list[float], expected_status: str
) -> None:
    reports = {"baseline": _report(), "candidate": _report()}
    name = "browser_style_layout"
    for variant, values in (("baseline", baseline), ("candidate", candidate)):
        for run, value in zip(reports[variant]["journeys"][name]["runs"], values, strict=True):
            run["p50_ms"] = run["p95_ms"] = value
    failures, warnings, rows = assess_reports(reports, _args())
    row = next(row for row in rows if row["journey"] == name)
    assert row["status"] == expected_status
    assert bool(failures) == (expected_status == "regression")
    if expected_status == "advisory" or candidate == [150, 150, 150]:
        assert warnings


def test_absolute_noise_floor_applies_even_above_budget() -> None:
    reports = {"baseline": _report(), "candidate": _report()}
    for variant, value in (("baseline", 16), ("candidate", 20)):
        for run in reports[variant]["journeys"]["browser_style_layout"]["runs"]:
            run["p50_ms"] = run["p95_ms"] = value
    args = _args()
    args.threshold = 0.1
    failures, warnings, _ = assess_reports(reports, args)
    assert not failures
    assert warnings


def test_standalone_runs_still_enforce_budgets() -> None:
    report = _report()
    for run in report["journeys"]["browser_style_layout"]["runs"]:
        run["p95_ms"] = 17
    failures, warnings, rows = assess_reports({"candidate": report}, _args())
    assert failures == ["browser_style_layout: P95 17.00 ms exceeds 16.00 ms"]
    assert warnings == rows == []


def test_paired_gate_requires_consistency_in_the_same_percentile() -> None:
    reports = {"baseline": _report(), "candidate": _report()}
    base = reports["baseline"]["journeys"]["browser_style_layout"]["runs"]
    candidate = reports["candidate"]["journeys"]["browser_style_layout"]["runs"]
    for row, p50, p95 in zip(base, [5, 50, 5], [50, 50, 5], strict=True):
        row["p50_ms"], row["p95_ms"] = p50, p95
    for row, p50, p95 in zip(candidate, [50, 50, 5], [50, 50, 50], strict=True):
        row["p50_ms"], row["p95_ms"] = p50, p95
    failures, warnings, rows = assess_reports(reports, _args())
    assert not failures
    assert warnings
    row = next(row for row in rows if row["journey"] == "browser_style_layout")
    assert row["regressing_pairs"] == {"p50": 1, "p95": 1}


def test_session_open_regression_is_gated_with_one_sample_per_run() -> None:
    reports = {"baseline": _report(), "candidate": _report()}
    for row in reports["candidate"]["journeys"]["browser_session_open"]["runs"]:
        row["p50_ms"] = row["p95_ms"] = 3000
    failures, _, rows = assess_reports(reports, _args())
    assert len(failures) == 1
    row = next(row for row in rows if row["journey"] == "browser_session_open")
    assert row["status"] == "regression"
    assert row["regressing_pairs"] == {"p50": 3}


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--iterations", "0"),
        ("--runs", "0"),
        ("--warmup", "-1"),
        ("--cpu-throttle", "nan"),
        ("--cpu-throttle", "0.5"),
        ("--threshold", "inf"),
        ("--min-regression-ms", "-1"),
        ("--max-style-layout-ms", "nan"),
        ("--max-key-to-frame-ms", "0"),
    ],
)
def test_ui_cli_rejects_invalid_counts_and_budgets(option: str, value: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        parse_args([option, value])


def test_ui_cli_requires_both_bundles(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--web-dist", str(tmp_path), "--baseline-dist", str(tmp_path / "missing")])


def test_paired_comparisons_require_repeated_runs(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--web-dist", str(tmp_path), "--baseline-dist", str(tmp_path), "--runs", "1"])


@pytest.mark.parametrize("regression", [False, True])
async def test_orchestration_alternates_bundles_and_propagates_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, regression: bool
) -> None:
    args = _args()
    args.output_dir = tmp_path / "results"
    args.web_dist, args.baseline_dist = Path("candidate"), Path("baseline")
    args.revision, args.baseline_revision = "candidate-sha", "baseline-sha"
    browser = SimpleNamespace(version="test-chromium", close=AsyncMock())

    @contextlib.asynccontextmanager
    async def playwright_context():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    @contextlib.asynccontextmanager
    async def environment(dist):
        yield SimpleNamespace(dist=dist)

    calls = []
    sample = _report()["samples"]["browser"][0]

    async def scenario(_browser, env, _session_id, mode, _args, _evidence):
        calls.append((env.dist.name, mode))
        result = copy.deepcopy(sample)
        if regression and env.dist == args.web_dist:
            result["style_layout"] = [150.0] * args.iterations
        return result

    monkeypatch.setattr(ui_run, "async_playwright", playwright_context)
    monkeypatch.setattr(ui_run, "UIEnvironment", environment)
    monkeypatch.setattr(ui_run, "seed_conversation", AsyncMock(return_value="session"))
    monkeypatch.setattr(ui_run, "measure_scenario", scenario)
    assert await ui_run.run_benchmark(args) is not regression
    assert calls == [
        (variant, mode)
        for variant in ("baseline", "candidate", "candidate", "baseline", "baseline", "candidate")
        for mode in ("browser", "mac_css")
    ]
    comparison = json.loads((args.output_dir / "comparison.json").read_text())
    assert comparison["passed"] is not regression
    assert bool(comparison["failures"]) is regression
    assert (
        (args.output_dir / "summary.md").read_text().endswith("FAIL\n" if regression else "PASS\n")
    )
    for variant in ("candidate", "baseline"):
        report = json.loads((args.output_dir / f"{variant}.json").read_text())
        assert report["git_sha"] == f"{variant}-sha"
        assert not measurement_failures(report, args)
    browser.close.assert_awaited_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handling")
def test_sigterm_finishes_teardown_and_reports_error(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    script = """
import asyncio
import sys
from pathlib import Path
from dev.benchmarks.ui import run

root = Path(sys.argv[1])

async def pending_benchmark(args):
    try:
        (root / 'ready').touch()
        await asyncio.Event().wait()
    finally:
        (root / 'closing').touch()
        await asyncio.sleep(0.5)
        (root / 'closed').touch()

run.run_benchmark = pending_benchmark
raise SystemExit(run.main(['--web-dist', str(root), '--output-dir', str(root)]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for sentinel in ("ready", "closing"):
            deadline = time.monotonic() + 10
            while not (tmp_path / sentinel).exists() and time.monotonic() < deadline:
                assert process.poll() is None, process.communicate()
                time.sleep(0.01)
            assert (tmp_path / sentinel).exists()
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 128 + signal.SIGTERM, (stdout, stderr)
        assert (tmp_path / "closed").exists()
        assert "CancelledError" in (tmp_path / "error.txt").read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
