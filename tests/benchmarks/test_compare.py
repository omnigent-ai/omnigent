from __future__ import annotations

import io

import pytest
from rich.console import Console

from dev.benchmarks.omnigent import compare
from dev.benchmarks.omnigent.compare import build_markdown, compare_reports, print_table


def _journey(p50: list[float], p95: list[float], *, n_success: int = 100) -> dict:
    return {
        "backend": "sqlite",
        "runs": [
            {"n_success": n_success, "p50_ms": run_p50, "p95_ms": run_p95}
            for run_p50, run_p95 in zip(p50, p95, strict=True)
        ],
        "summary": {
            "avg_p50_ms": sum(p50) / len(p50),
            "avg_p95_ms": sum(p95) / len(p95),
        },
    }


_FAILED_RUN = {"n_success": 0, "n_failures": 100, "p50_ms": 0.0, "p95_ms": 0.0}


def _stalled_interrupt(*, baseline_n: int, candidate_n: int) -> tuple[dict, dict]:
    """A flat-p50 interrupt journey whose candidate has one slow sample per run."""
    baseline = {"interrupt": _journey([95, 99, 101], [99.1, 99.1, 99.1], n_success=baseline_n)}
    candidate = {"interrupt": _journey([90, 95, 98], [100, 580.8, 590], n_success=candidate_n)}
    return {"journeys": baseline}, {"journeys": candidate}


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


@pytest.mark.parametrize(("baseline_n", "candidate_n"), [(5, 5), (100, 5), (5, 100)])
def test_small_sample_runs_on_either_side_gate_on_p50_only(
    baseline_n: int, candidate_n: int
) -> None:
    # Below 20 samples a run's p95 is its slowest sample, so one stall in the
    # median run must not fail a journey whose p50 is flat.
    baseline, candidate = _stalled_interrupt(baseline_n=baseline_n, candidate_n=candidate_n)

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "ok"
    assert rows[0]["p95_gated"] is False
    assert rows[0]["delta_p95"] > 1.0


def test_one_under_sampled_run_turns_off_the_p95_gate() -> None:
    baseline = {"journeys": {"warm_turn": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"warm_turn": _journey([100, 101, 102], [300, 310, 320])}}
    candidate["journeys"]["warm_turn"]["runs"][1]["n_success"] = 19

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["p95_gated"] is False


def test_small_sample_runs_still_gate_on_p50() -> None:
    baseline = {"journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130], n_success=5)}}
    candidate = {
        "journeys": {"interrupt": _journey([250, 260, 270], [300, 310, 320], n_success=5)}
    }

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert not passed
    assert rows[0]["status"] == "regression"


@pytest.mark.parametrize(("n_success", "passed_expected"), [(19, True), (20, False)])
def test_p95_gate_needs_twenty_samples_per_run(n_success: int, passed_expected: bool) -> None:
    baseline = {
        "journeys": {"warm_turn": _journey([100, 101, 102], [120, 125, 130], n_success=n_success)}
    }
    candidate = {
        "journeys": {"warm_turn": _journey([100, 101, 102], [300, 310, 320], n_success=n_success)}
    }

    passed, _ = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed is passed_expected


def test_fully_failed_runs_do_not_enter_the_comparison() -> None:
    baseline = {"journeys": {"list_sessions": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"list_sessions": _journey([110, 111, 112], [125, 130, 135])}}
    candidate["journeys"]["list_sessions"]["runs"].insert(0, dict(_FAILED_RUN))

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["c_p50"] == 111
    assert rows[0]["c_p95"] == 130
    assert rows[0]["p95_gated"] is True


def test_all_runs_failed_is_reported_as_skipped() -> None:
    baseline = {"journeys": {"list_sessions": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {
        "journeys": {
            "list_sessions": {"backend": "sqlite", "runs": [dict(_FAILED_RUN)] * 3, "summary": {}}
        }
    }

    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert passed
    assert rows[0]["status"] == "skipped"
    assert rows[0]["c_p50"] is None


def test_markdown_marks_ungated_p95_deltas() -> None:
    baseline, candidate = _stalled_interrupt(baseline_n=5, candidate_n=5)
    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    markdown = build_markdown(rows, threshold=1.0, passed=passed)

    assert "**PASS**" in markdown
    assert "%† |" in markdown
    assert "P95 not gated" in markdown


def test_markdown_omits_the_p95_note_when_every_run_is_well_sampled() -> None:
    baseline = {"journeys": {"list_sessions": _journey([100, 101, 102], [120, 125, 130])}}
    candidate = {"journeys": {"list_sessions": _journey([110, 111, 112], [125, 130, 135])}}
    passed, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    markdown = build_markdown(rows, threshold=1.0, passed=passed)

    assert "†" not in markdown


def test_table_marks_only_ungated_p95_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline, candidate = _stalled_interrupt(baseline_n=5, candidate_n=5)
    baseline["journeys"]["list_sessions"] = _journey([100, 101, 102], [120, 125, 130])
    candidate["journeys"]["list_sessions"] = _journey([110, 111, 112], [125, 130, 135])
    _, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    monkeypatch.setattr(compare, "console", console)

    print_table(rows, threshold=1.0)

    lines = console.file.getvalue().splitlines()
    interrupt = next(line for line in lines if line.lstrip().startswith("interrupt"))
    list_sessions = next(line for line in lines if line.lstrip().startswith("list_sessions"))
    assert "+486.1%†" in interrupt
    assert "†" not in list_sessions
    assert any("P95 not gated" in line for line in lines)


@pytest.mark.parametrize(
    ("n_success", "p50", "expected_cells"),
    [
        # A five-sample journey failing on p50 keeps its p95 delta visible but unpainted.
        (5, [250, 260, 270], ("[red]+157.4%[/red]", "+148.0%†")),
        # A well-sampled journey failing on p95 paints only that cell.
        (100, [110, 111, 112], ("+9.9%", "[red]+148.0%[/red]")),
    ],
)
def test_table_cells_highlight_only_the_gated_metric_of_a_failing_row(
    n_success: int, p50: list[float], expected_cells: tuple[str, str]
) -> None:
    baseline = {
        "journeys": {"interrupt": _journey([100, 101, 102], [120, 125, 130], n_success=n_success)}
    }
    candidate = {"journeys": {"interrupt": _journey(p50, [300, 310, 320], n_success=n_success)}}
    _, rows = compare_reports(baseline, candidate, threshold=1.0, backend="sqlite")

    assert rows[0]["status"] == "regression"
    assert compare._delta_cells(rows[0], threshold=1.0) == expected_cells
