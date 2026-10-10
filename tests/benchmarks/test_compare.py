from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev.benchmarks.omnigent.compare import (
    build_markdown,
    compare_reports,
    main,
    unmeasured_journeys,
)


def _journey(p50: list[float], p95: list[float], n: int | None = None) -> dict:
    count = {} if n is None else {"n_success": n}
    return {
        "backend": "sqlite",
        "runs": [
            {"p50_ms": run_p50, "p95_ms": run_p95, **count}
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


def test_compare_does_not_gate_p95_on_small_sample_journeys() -> None:
    # With 5 samples per run, P95 is the slowest sample: one stall is not a regression.
    baseline = {"journeys": {"interrupt": _journey([220, 227, 208], [256, 232, 306], n=5)}}
    candidate = {"journeys": {"interrupt": _journey([240, 258, 204], [266, 2057, 528], n=5)}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "ok"
    assert rows[0]["p95_gated"] is False
    assert rows[0]["delta_p95"] > 1.0
    markdown = build_markdown(rows, threshold=1.0, passed=passed)
    assert "+106.2% †" in markdown
    assert "† P95 not gated" in markdown


def test_compare_still_gates_p50_on_small_sample_journeys() -> None:
    baseline = {"journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130], n=5)}}
    candidate = {"journeys": {"interrupt": _journey([250, 251, 252], [260, 265, 270], n=5)}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "regression"


def test_compare_gates_p95_with_enough_samples_on_both_sides() -> None:
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [12, 12, 12], n=100)}}
    candidate = {"journeys": {"list_sessions": _journey([11, 11, 11], [30, 30, 30], n=100)}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["p95_gated"] is True
    assert "†" not in build_markdown(rows, threshold=1.0, passed=passed)


def test_compare_skips_p95_gate_when_one_side_is_small() -> None:
    baseline = {"journeys": {"warm_turn": _journey([100, 100, 100], [120, 120, 120], n=100)}}
    candidate = {"journeys": {"warm_turn": _journey([105, 105, 105], [400, 400, 400], n=5)}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["p95_gated"] is False


def _skipped_journey() -> dict:
    """A journey that errored out of measurement: no runs, no metrics."""
    return {"backend": "sqlite", "runs": [], "summary": {}, "skipped": True}


@pytest.mark.parametrize(
    ("baseline_journey", "candidate_journey", "unmeasured"),
    [
        pytest.param(
            _journey([100, 101, 102], [120, 125, 130]),
            _journey([100, 101, 102], [120, 125, 130]),
            [],
            id="measured-both-sides",
        ),
        pytest.param(
            _journey([100, 101, 102], [120, 125, 130]),
            _skipped_journey(),
            ["interrupt"],
            id="missing-candidate-metrics",
        ),
        pytest.param(
            None,
            _journey([100, 101, 102], [120, 125, 130]),
            ["interrupt"],
            id="missing-baseline-journey",
        ),
        pytest.param(
            {"backend": "sqlite", "runs": [], "summary": {}},
            _journey([100, 101, 102], [120, 125, 130]),
            ["interrupt"],
            id="missing-baseline-metrics",
        ),
    ],
)
def test_a_recheck_must_measure_the_flagged_journey_on_both_sides(
    baseline_journey: dict | None, candidate_journey: dict, unmeasured: list[str]
) -> None:
    """A confirmation that never measured the flagged journey cannot clear it."""
    baseline = {"journeys": {} if baseline_journey is None else {"interrupt": baseline_journey}}
    candidate = {"journeys": {"interrupt": candidate_journey}}

    _, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert unmeasured_journeys(rows, ["interrupt"]) == unmeasured


def test_a_required_journey_absent_from_both_reports_is_unmeasured() -> None:
    report = {"journeys": {"list_sessions": _journey([10, 10, 10], [12, 12, 12])}}

    _, rows = compare_reports(report, report, threshold=1.0, backend="sqlite")

    assert unmeasured_journeys(rows, ["interrupt", "list_sessions"]) == ["interrupt"]


def _write_reports(tmp_path: Path, baseline: dict, candidate: dict) -> list[str]:
    (tmp_path / "b.json").write_text(json.dumps(baseline))
    (tmp_path / "c.json").write_text(json.dumps(candidate))
    return ["--baseline", str(tmp_path / "b.json"), "--candidate", str(tmp_path / "c.json")]


def test_cli_exits_3_and_reports_unmeasured_required_journeys(tmp_path: Path) -> None:
    baseline = {"journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"interrupt": _skipped_journey()}}
    args = _write_reports(tmp_path, baseline, candidate)
    out = tmp_path / "out.json"
    md = tmp_path / "out.md"

    rc = main(
        [*args, "--require", "interrupt", "--output-json", str(out), "--output-markdown", str(md)]
    )

    assert rc == 3
    assert json.loads(out.read_text())["unmeasured"] == ["interrupt"]
    assert "**INCOMPLETE** — not measured on both sides: interrupt." in md.read_text()
    # Without --require the same comparison passes, as before.
    assert main(args) == 0


def test_cli_a_regression_outranks_an_incomplete_recheck(tmp_path: Path) -> None:
    baseline = {
        "journeys": {
            "interrupt": _journey([100, 101, 102], [120, 125, 130]),
            "list_sessions": _journey([10, 10, 10], [12, 12, 12]),
        }
    }
    candidate = {
        "journeys": {
            "interrupt": _skipped_journey(),
            "list_sessions": _journey([30, 30, 30], [36, 36, 36]),
        }
    }

    assert main([*_write_reports(tmp_path, baseline, candidate), "--require", "interrupt"]) == 1


def test_cli_writes_incomplete_markdown_when_no_rows(tmp_path: Path) -> None:
    args = _write_reports(tmp_path, {"journeys": {}}, {"journeys": {}})
    md = tmp_path / "out.md"

    rc = main([*args, "--require", "interrupt", "--output-markdown", str(md)])

    assert rc == 3
    assert "**INCOMPLETE** — not measured on both sides: interrupt." in md.read_text()


def test_cli_output_json_lists_rows(tmp_path: Path) -> None:
    baseline = {"journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"interrupt": _journey([300, 301, 302], [320, 325, 330])}}
    out = tmp_path / "out.json"

    rc = main([*_write_reports(tmp_path, baseline, candidate), "--output-json", str(out)])

    data = json.loads(out.read_text())
    assert rc == 1
    assert data["passed"] is False
    assert [(r["journey"], r["status"]) for r in data["rows"]] == [("interrupt", "regression")]


def test_compare_applies_a_separate_p95_threshold() -> None:
    # P50 +20% stays under a 30% P50 threshold; P95 +50% stays under a 60% P95 one.
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [20, 20, 20], n=100)}}
    candidate = {"journeys": {"list_sessions": _journey([12, 12, 12], [30, 30, 30], n=100)}}

    passed, rows = compare_reports(
        baseline, candidate, threshold=0.3, backend="sqlite", threshold_p95=0.6
    )
    assert passed
    assert rows[0]["status"] == "ok"

    # Without the separate threshold, the same +50% P95 trips the 30% gate.
    passed, rows = compare_reports(baseline, candidate, threshold=0.3, backend="sqlite")
    assert not passed
    assert rows[0]["status"] == "regression"


def test_compare_p95_threshold_does_not_loosen_p50() -> None:
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [20, 20, 20], n=100)}}
    candidate = {"journeys": {"list_sessions": _journey([14, 14, 14], [21, 21, 21], n=100)}}

    passed, rows = compare_reports(
        baseline, candidate, threshold=0.3, backend="sqlite", threshold_p95=0.6
    )

    assert not passed  # P50 +40% > 30%
    assert rows[0]["status"] == "regression"


def test_markdown_states_both_thresholds() -> None:
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [20, 20, 20], n=100)}}
    passed, rows = compare_reports(
        baseline, baseline, threshold=0.3, backend="sqlite", threshold_p95=0.6
    )

    markdown = build_markdown(rows, threshold=0.3, passed=passed, threshold_p95=0.6)

    assert "**30%** on run-median P50, **60%** on P95" in markdown
    assert "**100%** on run-median P50 or P95" in build_markdown(
        rows, threshold=1.0, passed=passed
    )


def _failed_journey(runs: int = 3) -> dict:
    """A journey whose every op failed: runs exist, latencies are 0.0 placeholders."""
    return {
        "backend": "sqlite",
        "runs": [
            {"n_success": 0, "n_failures": 100, "p50_ms": 0.0, "p95_ms": 0.0} for _ in range(runs)
        ],
        "summary": {"runs_total": runs, "runs_ok": 0},
    }


def test_compare_fails_a_journey_whose_every_candidate_op_failed() -> None:
    # Both sides failed every request (all HTTP 500s); 0.0 ms is not a measurement.
    baseline = {"journeys": {"policy_evaluate": _failed_journey()}}
    candidate = {"journeys": {"policy_evaluate": _failed_journey()}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "failed"
    assert rows[0]["c_p50"] is None
    assert rows[0]["delta_p50"] is None
    markdown = build_markdown(rows, threshold=1.0, passed=passed)
    assert "| policy_evaluate | ❌ failed |" in markdown
    assert "**FAIL**" in markdown


def test_compare_fails_when_only_the_candidate_failed() -> None:
    baseline = {"journeys": {"policy_evaluate": _journey([10, 10, 10], [12, 12, 12], n=100)}}
    candidate = {"journeys": {"policy_evaluate": _failed_journey()}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "failed"
    assert rows[0]["b_p50"] == 10


def test_compare_ignores_failed_runs_in_the_run_median() -> None:
    # One run failed outright; its 0.0 placeholder must not drag the median down.
    candidate_journey = _journey([10, 10], [12, 12], n=100)
    candidate_journey["runs"].append({"n_success": 0, "p50_ms": 0.0, "p95_ms": 0.0})
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [12, 12, 12], n=100)}}
    candidate = {"journeys": {"list_sessions": candidate_journey}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "ok"
    assert rows[0]["c_p50"] == 10


def test_compare_treats_an_all_failed_baseline_as_new() -> None:
    baseline = {"journeys": {"policy_evaluate": _failed_journey()}}
    candidate = {"journeys": {"policy_evaluate": _journey([10, 10, 10], [12, 12, 12], n=100)}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "new"
    assert rows[0]["delta_p50"] is None
    assert rows[0]["c_p50"] == 10


def test_compare_still_reports_an_errored_journey_as_skipped() -> None:
    baseline = {"journeys": {"interrupt": _journey([10, 10, 10], [12, 12, 12], n=50)}}
    candidate = {
        "journeys": {
            "interrupt": {"backend": "sqlite", "runs": [], "summary": {}, "skipped": True}
        }
    }

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "skipped"


def test_a_failed_run_does_not_disable_p95_gating() -> None:
    # Two 100-sample runs regress P95 by 100%+; a third run failed outright.
    candidate_journey = _journey([10, 10], [30, 30], n=100)
    candidate_journey["runs"].append({"n_success": 0, "p50_ms": 0.0, "p95_ms": 0.0})
    baseline = {"journeys": {"list_sessions": _journey([10, 10, 10], [12, 12, 12], n=100)}}
    candidate = {"journeys": {"list_sessions": candidate_journey}}

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert rows[0]["p95_gated"] is True
    assert not passed
    assert rows[0]["status"] == "regression"
