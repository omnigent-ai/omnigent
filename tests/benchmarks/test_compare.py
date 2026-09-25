from __future__ import annotations

from dev.benchmarks.omnigent.compare import compare_reports


def _journey(p50: list[float], p95: list[float]) -> dict:
    return {
        "backend": "sqlite",
        "runs": [
            {"p50_ms": run_p50, "p95_ms": run_p95}
            for run_p50, run_p95 in zip(p50, p95, strict=True)
        ],
        "summary": {
            "avg_p50_ms": sum(p50) / len(p50),
            "avg_p95_ms": sum(p95) / len(p95),
        },
    }


def test_compare_uses_run_median_to_resist_one_outlier() -> None:
    baseline = {"journeys": {"interrupt": _journey([120, 121, 122], [125, 130, 135])}}
    candidate = {"journeys": {"interrupt": _journey([110, 111, 112], [120, 125, 720])}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["b_p95"] == 130
    assert rows[0]["c_p95"] == 125


def test_compare_flags_a_run_median_regression() -> None:
    baseline = {"journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"interrupt": _journey([110, 111, 112], [300, 310, 320])}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "regression"


def test_compare_falls_back_to_summary_for_legacy_reports() -> None:
    baseline = {
        "journeys": {
            "interrupt": {
                "backend": "sqlite",
                "summary": {"avg_p50_ms": 100.0, "avg_p95_ms": 125.0},
            }
        }
    }
    candidate = {
        "journeys": {
            "interrupt": {
                "backend": "sqlite",
                "summary": {"avg_p50_ms": 110.0, "avg_p95_ms": 300.0},
            }
        }
    }

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "regression"
