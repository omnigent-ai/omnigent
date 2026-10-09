"""The UI gate must fail closed on missing data and honor rendering budgets."""

from __future__ import annotations

import argparse
import copy
import math
from pathlib import Path

import pytest

from dev.benchmarks.ui.run import budget_failures, make_report, parse_args, required_journeys


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        iterations=20,
        runs=3,
        warmup=3,
        cpu_throttle=4,
        max_key_to_frame_ms=100,
        max_style_layout_ms=16,
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
@pytest.mark.parametrize("metric", ["key_to_frame", "style_layout"])
def test_ui_gate_requires_every_run_and_keystroke(mode: str, metric: str) -> None:
    name = f"{mode}_{metric}"
    report = _report()
    report["journeys"][name]["runs"].pop()
    assert budget_failures(report, _args()) == [f"{name}: incomplete samples"]

    report = _report()
    report["journeys"][name]["runs"][1]["n_success"] -= 1
    assert budget_failures(report, _args()) == [f"{name}: incomplete samples"]


@pytest.mark.parametrize("bad_value", [17.0, math.inf, math.nan])
def test_ui_gate_rejects_slow_or_invalid_rendering(bad_value: float) -> None:
    report = _report()
    for row in report["journeys"]["browser_style_layout"]["runs"]:
        row["p95_ms"] = bad_value
    failures = budget_failures(report, _args())
    assert len(failures) == 1
    assert "browser_style_layout: P95" in failures[0]


def test_one_slow_run_does_not_fail_the_ui_budget() -> None:
    report = _report()
    original = copy.deepcopy(report)
    report["journeys"]["browser_style_layout"]["runs"][0]["p95_ms"] = 200
    assert budget_failures(report, _args()) == []
    # The outlier remains visible in the report even though the median gates.
    assert report != original
    assert report["journeys"]["browser_style_layout"]["runs"][0]["p95_ms"] == 200


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
