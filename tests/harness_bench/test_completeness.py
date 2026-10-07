"""CLI checks must distinguish unmeasured capabilities from passing results.

These tests keep the real orchestration, probes, reconciliation, and report
writers together. Only the provider-facing driver is simulated; the missing
CLI test additionally exercises the installed entry point in a fresh process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness_bench.__main__ import main
from tests.harness_bench.bench import BenchMatrix, HarnessReport
from tests.harness_bench.driver import TurnResult
from tests.harness_bench.profile import resolve_profile


@pytest.mark.parametrize("mode", [[], ["--no-live"]])
def test_require_complete_needs_explicit_live(mode, capsys, monkeypatch):
    def unexpected_credentials(*args):
        pytest.fail("argument validation must precede credential resolution")

    monkeypatch.setattr(
        "tests.harness_bench.runtime_env.bench_creds_skip_reason", unexpected_credentials
    )
    assert main([*mode, "--require-complete"]) == 2
    assert "requires explicit --live" in capsys.readouterr().err


def test_empty_matrix_is_incomplete():
    assert not BenchMatrix(reports=[]).complete
    assert not BenchMatrix(reports=[HarnessReport(resolve_profile("codex"), cells=[])]).complete


@pytest.mark.parametrize(
    ("scenario", "code", "complete", "observed"),
    [
        ("success", 0, True, "supported"),
        ("unavailable", 2, False, "skipped"),
        ("startup-error", 2, False, "skipped"),
        ("timeout", 2, False, "skipped"),
        ("probe-error", 2, False, "unknown"),
        ("later-timeout", 2, False, "skipped"),
        ("mismatch", 1, True, "unsupported"),
        ("not-applicable", 0, True, "not_applicable"),
        ("p1-unobserved", 2, False, "skipped"),
    ],
)
def test_cli_complete_results_through_orchestration(
    scenario, code, complete, observed, monkeypatch, tmp_path, capsys
):
    class Driver:
        transport = "sdk-inproc"

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def unavailable(*args, **kwargs):
            return "test CLI absent" if scenario == "unavailable" else None

        async def __aenter__(self):
            if scenario == "startup-error":
                raise RuntimeError("test startup failed")
            return self

        async def __aexit__(self, *args):
            pass

        async def run_basic_turn(self, marker):
            if scenario == "probe-error":
                raise RuntimeError("test probe failed")
            return TurnResult(text=marker, completed=True, timed_out=scenario == "timeout")

        async def run_streaming_turn(self):
            return TurnResult(
                text="hello world",
                completed=True,
                text_delta_count=0 if scenario == "mismatch" else 2,
                timed_out=scenario == "later-timeout",
            )

        async def run_reasoning_turn(self):
            return TurnResult(completed=True)

    monkeypatch.setenv("OPENAI_API_KEY", "local-fake-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr("tests.harness_bench.bench.resolve_driver_class", lambda *a, **k: Driver)
    report = tmp_path / "report.json"
    args = [
        "--live",
        "--require-complete",
        "--harness",
        "codex",
        "--dimension",
        {"not-applicable": "omnigent_mcp", "p1-unobserved": "reasoning"}.get(
            scenario, "streaming"
        ),
        "--no-rich",
        "--json",
        "--report",
        str(report),
    ]
    assert main(args) == code
    payload = json.loads(report.read_text())
    assert capsys.readouterr().out.rstrip().endswith(report.read_text().rstrip())
    assert payload["complete"] is complete
    assert payload["has_drift"] is (scenario == "mismatch")
    assert payload["harnesses"][0]["cells"][-1]["observed"] == (
        "skipped" if scenario == "probe-error" else observed
    )
    if scenario == "probe-error":
        assert payload["harnesses"][0]["cells"][0]["observed"] == "unknown"

    # Browsing the matrix remains allowed even on an unprovisioned machine.
    args.remove("--require-complete")
    assert main(args) == (1 if scenario == "mismatch" else 0)


def test_offline_report_labels_declarations_and_incomplete_coverage(tmp_path, capsys):
    report = tmp_path / "report.md"
    assert main(["--no-live", "--harness", "codex", "--report", str(report)]) == 0
    for output in (report.read_text(), capsys.readouterr().out):
        assert "declared, not observed" in output
        assert "Coverage: incomplete" in output


@pytest.mark.parametrize("require_complete", [False, True])
def test_missing_cli_real_process(require_complete, tmp_path):
    # An allowlisted environment ensures no ambient user provider or CLI is used.
    env = {
        "PATH": str(tmp_path),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
        "OPENAI_API_KEY": "local-fake-key",
        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
    }
    args = [
        sys.executable,
        "-m",
        "tests.harness_bench",
        "--live",
        "--fast",
        "--harness",
        "codex",
        "--dimension",
        "basic_turn",
        "--no-rich",
        "--json",
    ]
    if require_complete:
        args.append("--require-complete")
    result = subprocess.run(
        args,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (2 if require_complete else 0), result.stderr
    payload = json.loads(result.stdout)
    assert payload["complete"] is False
    assert payload["has_drift"] is False
    row = payload["harnesses"][0]
    assert "codex" in row["skipped_reason"]
    assert row["cells"][0]["observed"] == "skipped"
