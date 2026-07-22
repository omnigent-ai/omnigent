"""Smoke tests for the end-to-end UI benchmark harness (dev/benchmarks/omnigent_ui).

The pure layers — path normalization, network aggregation, iteration clamping,
and the report-block shape — get direct unit checks that run on the normal CI
lane (no browser, no server, no creds).

The full browser end-to-end test needs a built SPA and an installed Playwright
Chromium, which the normal lane lacks, so it auto-skips unless both are present
(run it locally after ``uv sync --extra e2e-ui`` + ``playwright install
chromium``). CI exercises the full path nightly via e2e-ui-benchmark.yml.
"""

from __future__ import annotations

import argparse
import shutil
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from dev.benchmarks.omnigent.measure import RunResult, aggregate
from dev.benchmarks.omnigent.schema import SCHEMA_VERSION, build_report
from dev.benchmarks.omnigent_ui import run as ui_run
from dev.benchmarks.omnigent_ui.environment import _BUILD_OUTPUT
from dev.benchmarks.omnigent_ui.journeys import (
    ALL_UI_JOURNEYS,
    build_network_block,
    resolve_ui_journeys,
    summarize_browser_timing,
)
from dev.benchmarks.omnigent_ui.netcapture import (
    RepCapture,
    aggregate_network,
    normalize_path,
)

_ALL = ["landing_load", "new_session_first_token", "switch_sessions", "fork_session"]


def _d(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


# ── path normalization ───────────────────────────────────────


def test_normalize_path_collapses_hex_session_ids() -> None:
    sid = "a" * 32
    assert normalize_path(f"http://h/v1/sessions/{sid}") == "/v1/sessions/:id"
    assert (
        normalize_path(f"http://h/v1/sessions/{sid}/items?order=asc") == "/v1/sessions/:id/items"
    )


def test_normalize_path_collapses_uuid_and_prefixed_ids() -> None:
    uuid = "12345678-1234-1234-1234-1234567890ab"
    assert normalize_path(f"http://h/v1/hosts/{uuid}/filesystem") == "/v1/hosts/:id/filesystem"
    assert normalize_path("http://h/v1/agents/ag_ab12cd34") == "/v1/agents/:id"
    assert normalize_path("http://h/v1/sessions/conv_xy99/events") == "/v1/sessions/:id/events"


def test_normalize_path_dehashes_vite_assets_and_drops_query() -> None:
    assert normalize_path("http://h/assets/index-a1b2c3d4.js") == "/assets/index.js"
    assert normalize_path("http://h/assets/main-deadbeef99.css?v=2") == "/assets/main.css"
    # Real rolldown shapes seen in the built SPA: mixed-case + underscore, and
    # double-hashed chunks. Both collapse to the stable stem so asset counts
    # aggregate across deploys (each build re-hashes them).
    assert normalize_path("http://h/assets/bundle-mjs-B_AA55HL.js") == "/assets/bundle-mjs.js"
    assert normalize_path("http://h/assets/chunk-CSCIHK7Q-Cqn_ZLae.js") == "/assets/chunk.js"
    assert normalize_path("http://h/assets/useQuery-B4Bqc_LO.js") == "/assets/useQuery.js"


def test_normalize_path_keeps_unhashed_asset_names() -> None:
    # A plain lowercase word suffix is NOT a build fingerprint — leave it.
    assert normalize_path("http://h/assets/jsx-runtime.js") == "/assets/jsx-runtime.js"
    assert normalize_path("http://h/assets/preload-helper.js") == "/assets/preload-helper.js"
    assert normalize_path("http://h/assets/react-dom.js") == "/assets/react-dom.js"


def test_normalize_path_root_and_numeric() -> None:
    assert normalize_path("http://h/") == "/"
    assert normalize_path("http://h/v1/items/42") == "/v1/items/:n"


# ── network aggregation ──────────────────────────────────────


def test_aggregate_network_medians_across_reps() -> None:
    reps = [
        RepCapture(
            by_resource_type=Counter({"document": 1, "script": 6}),
            by_endpoint=Counter({"GET /v1/sessions/:id": 2}),
            total=9,
        ),
        RepCapture(
            by_resource_type=Counter({"document": 1, "script": 6}),
            by_endpoint=Counter({"GET /v1/sessions/:id": 2}),
            total=9,
        ),
        RepCapture(
            by_resource_type=Counter({"document": 1, "script": 8}),
            by_endpoint=Counter({"GET /v1/sessions/:id": 3}),
            total=12,
        ),
    ]
    block = aggregate_network(reps)
    assert block["aggregation"] == "median_per_rep"
    assert block["reps"] == 3
    assert _d(block["by_resource_type"])["script"] == 6  # median(6,6,8)
    assert _d(block["by_endpoint"])["GET /v1/sessions/:id"] == 2  # median(2,2,3)
    assert block["median_total_requests"] == 9
    assert block["max_total_requests"] == 12
    assert block["per_rep_total_requests"] == [9, 9, 12]


def test_aggregate_network_missing_key_counts_as_zero() -> None:
    # A key present in only one of three reps has median 0 — it fires rarely.
    reps = [
        RepCapture(by_endpoint=Counter({"GET /v1/info": 1}), total=1),
        RepCapture(total=0),
        RepCapture(total=0),
    ]
    block = aggregate_network(reps)
    assert _d(block["by_endpoint"])["GET /v1/info"] == 0


def test_aggregate_network_empty() -> None:
    block = aggregate_network([])
    assert block["reps"] == 0
    assert block["by_resource_type"] == {}
    assert block["per_rep_total_requests"] == []


# ── iteration clamp + registry ───────────────────────────────


def test_effective_iterations_clamps_down_not_up() -> None:
    journey = ALL_UI_JOURNEYS["landing_load"]
    assert ui_run._effective_iterations(journey, 100) == journey.max_iterations
    assert ui_run._effective_iterations(journey, 1) == 1


def test_resolve_ui_journeys_all_and_named() -> None:
    assert [j.name for j in resolve_ui_journeys(None)] == _ALL
    assert [j.name for j in resolve_ui_journeys(["fork_session"])] == ["fork_session"]
    with pytest.raises(KeyError):
        resolve_ui_journeys(["nope"])


def test_backend_of_classifies_uri_schemes() -> None:
    assert ui_run._backend_of(None) == "sqlite"
    assert ui_run._backend_of("postgresql+psycopg://u@h/db") == "postgres"
    assert ui_run._backend_of("mysql+mysqldb://u@h/db") == "mysql"


# ── report block shape ───────────────────────────────────────


def test_report_block_carries_network_and_summary() -> None:
    """A journey block folds latency summary + a network block under schema v4."""
    block = aggregate([RunResult(latencies_ms=[400.0, 420.0], wall_time=1.0)])
    block["kind"] = "ui-latency"
    block["network"] = build_network_block(
        [RepCapture(by_resource_type=Counter({"document": 1}), total=1)]
    )
    report = build_report(
        {"landing_load": block},
        generated_at="2026-07-20T00:00:00+00:00",
        config={"iterations": 8},
        harness="web-ui-playwright",
    )
    assert report["schema_version"] == SCHEMA_VERSION == 5
    assert report["harness"] == "web-ui-playwright"
    landing = _d(_d(report["journeys"])["landing_load"])
    assert landing["kind"] == "ui-latency"
    assert set(_d(landing["summary"])) >= {"avg_p50_ms", "avg_p95_ms", "avg_p99_ms"}
    assert _d(_d(landing["network"])["by_resource_type"])["document"] == 1


def test_summarize_browser_timing_means() -> None:
    samples = [
        {"first_contentful_paint_ms": 200.0, "dom_content_loaded_ms": 180.0},
        {"first_contentful_paint_ms": 220.0, "dom_content_loaded_ms": 200.0},
    ]
    out = summarize_browser_timing(samples)
    assert out["first_contentful_paint_ms"] == 210.0
    assert out["dom_content_loaded_ms"] == 190.0
    assert summarize_browser_timing([]) == {}


# ── full browser end-to-end (auto-skips without a build + chromium) ──


def _chromium_available() -> bool:
    """Whether Playwright's Chromium is importable and installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


_HAS_SPA_BUILD = (_BUILD_OUTPUT / "index.html").is_file()
_HAS_NPM = shutil.which("npm") is not None


@pytest.mark.timeout(300)
@pytest.mark.skipif(
    not (_HAS_SPA_BUILD or _HAS_NPM),
    reason="no SPA build present and npm unavailable to build one",
)
@pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
async def test_ui_benchmark_smoke_end_to_end() -> None:
    """Boot server+runner+SPA, run one journey once in a real browser, check the report."""
    args = argparse.Namespace(
        journeys=["landing_load"],
        database_uri=None,
        iterations=1,
        runs=1,
        warmup=0,
        headed=False,
        skip_build=_HAS_SPA_BUILD,
        output=None,
        max_p50_ms=None,
        max_p99_ms=None,
    )
    report, passed = await ui_run.run_benchmark(args)

    assert passed
    assert report["schema_version"] == 5
    assert report["harness"] == "web-ui-playwright"
    landing = _d(_d(report["journeys"])["landing_load"])
    run_rows = cast(list[dict[str, object]], landing["runs"])
    assert run_rows, "landing_load produced no runs"
    assert run_rows[0]["n_failures"] == 0, run_rows[0]["failures"]
    assert cast(float, _d(landing["summary"])["avg_p50_ms"]) > 0.0
    assert _d(landing["network"])["reps"] == 1
    # The landing page load must fetch at least the document + some assets.
    assert cast(int, _d(landing["network"])["max_total_requests"]) >= 1
