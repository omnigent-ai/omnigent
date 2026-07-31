"""Unit tests for the dev load-test harness's pure helpers.

Deterministic, no server boot / no network — the fast regression net for the
``dev/loadtest/`` tooling. A full server-booting e2e would add flaky CI surface
for a dev-only tool; instead these cover the logic most likely to silently
break under maintenance: URL / env / argv wiring and result formatting. Mirrors
``tests/benchmarks/test_benchmark_smoke.py``, which likewise unit-tests the pure
layers "without paying the server-boot cost".

``run.py`` is stdlib-only so it imports unconditionally; ``ws_load_test.py``
imports ``locust`` + ``websocket-client`` at module load, so its helper tests
``importorskip`` those (skip cleanly on a CI lane without the ``loadtest`` extra).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# run.py is loaded by path so the test needs no package wiring for dev/loadtest.
_RUN_PY = Path(__file__).resolve().parents[2] / "dev" / "loadtest" / "run.py"


def _load_run_module():
    """Import dev/loadtest/run.py as a module (stdlib-only, always importable)."""
    spec = importlib.util.spec_from_file_location("_loadtest_run", _RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run_module()


# ── run.py: _fmt_num ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14.508", "14.5"),
        ("0", "0.0"),
        ("", "-"),
        ("3.6", "3.6"),  # a Requests/s value — same formatter, not just ms
        ("N/A", "N/A"),  # non-numeric passes through unchanged
    ],
)
def test_fmt_num(raw: str, expected: str) -> None:
    assert run._fmt_num(raw) == expected


# ── run.py: _build_env (flag → env wiring the locustfile reads) ───────


def _args(**over: object):
    """Build a parsed-args namespace with all run.py fields, overridable."""
    import argparse

    base = {
        "server": "http://localhost:8000",
        "host_id": None,
        "users": 10,
        "spawn_rate": 5.0,
        "run_time": "30s",
        "locustfile": str(_RUN_PY.with_name("ws_load_test.py")),
        "auth_token": None,
        "session_ids": None,
        "mount_prefix": "",
        "out_dir": None,
        "web": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_build_env_omits_unset_and_sets_present() -> None:
    env = run._build_env(_args())
    for k in ("HOST_ID", "AUTH_TOKEN", "SESSION_IDS", "MOUNT_PREFIX"):
        assert k not in env  # unset flags must not leak empty env vars

    env = run._build_env(
        _args(host_id="host_x", auth_token="tok", session_ids="a,b", mount_prefix="/omnigent")
    )
    assert env["HOST_ID"] == "host_x"
    assert env["AUTH_TOKEN"] == "tok"
    assert env["SESSION_IDS"] == "a,b"
    assert env["MOUNT_PREFIX"] == "/omnigent"


# ── run.py: _build_locust_argv (uses the current interpreter, not PATH) ─


def test_build_locust_argv_uses_current_interpreter_headless(tmp_path: Path) -> None:
    import sys

    argv = run._build_locust_argv(_args(users=7, spawn_rate=2.0, run_time="45s"), tmp_path)
    # Must launch `<python> -m locust`, never a bare `locust` off PATH.
    assert argv[:3] == [sys.executable, "-m", "locust"]
    assert "--host" in argv and "http://localhost:8000" in argv
    assert argv[argv.index("-u") + 1] == "7"
    assert argv[argv.index("-r") + 1] == "2.0"
    assert argv[argv.index("-t") + 1] == "45s"
    assert "--headless" in argv
    assert "--csv" in argv and "--html" in argv


def test_build_locust_argv_web_mode_skips_report_flags(tmp_path: Path) -> None:
    argv = run._build_locust_argv(_args(web=True), tmp_path)
    assert "--headless" not in argv
    assert "--csv" not in argv and "--html" not in argv


# ── run.py: _read_stats + _write_summary (result formatting) ──────────


def test_read_stats_missing_file_is_empty(tmp_path: Path) -> None:
    assert run._read_stats(tmp_path / "nope.csv") == []


def test_write_summary_dedupes_aggregated_row(tmp_path: Path) -> None:
    # Two per-type rows + locust's Aggregated row (already their sum). The
    # headline must count only the per-type rows (30 + 20 = 50), NOT also the
    # Aggregated row — otherwise the total double-counts.
    rows = [
        _stats_row("connect", count=30, fails=1),
        _stats_row("watch roundtrip", count=20, fails=1),
        _stats_row("Aggregated", count=50, fails=2),
    ]
    run._write_summary(tmp_path, _args(), rows, exit_code=1)
    text = (tmp_path / "summary.md").read_text()
    assert "50 requests, 2 failures" in text  # 50, not 100
    assert "FAIL" in text  # exit_code != 0 → FAIL outcome
    assert "| connect |" in text and "| watch roundtrip |" in text


def test_write_summary_pass_outcome(tmp_path: Path) -> None:
    run._write_summary(tmp_path, _args(), [_stats_row("connect", count=10, fails=0)], exit_code=0)
    assert "PASS" in (tmp_path / "summary.md").read_text()


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


# ── ws_load_test.py: _ws_url + _read_timeout_from_env ─────────────────


def _load_ws_module():
    """Import ws_load_test.py, skipping if the loadtest extra isn't installed."""
    pytest.importorskip("websocket", reason="loadtest extra (websocket-client) not installed")
    pytest.importorskip("locust", reason="loadtest extra (locust) not installed")
    spec = importlib.util.spec_from_file_location(
        "_loadtest_ws", _RUN_PY.with_name("ws_load_test.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("host", "prefix", "expected"),
    [
        ("http://localhost:8000", "", "ws://localhost:8000/v1/sessions/updates"),
        ("https://x.example.com", "", "wss://x.example.com/v1/sessions/updates"),
        ("https://x.example.com/", "", "wss://x.example.com/v1/sessions/updates"),
        # schemeless host (Locust accepts bare host:port) → ws://, not invalid
        ("localhost:8000", "", "ws://localhost:8000/v1/sessions/updates"),
        # reverse-proxy sub-path, with/without slashes normalized
        ("https://p.example.com", "/omnigent", "wss://p.example.com/omnigent/v1/sessions/updates"),
        ("https://p.example.com", "omnigent/", "wss://p.example.com/omnigent/v1/sessions/updates"),
    ],
)
def test_ws_url(host: str, prefix: str, expected: str) -> None:
    ws = _load_ws_module()
    assert ws._ws_url(host, prefix) == expected


def test_read_timeout_from_env_falls_back_on_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _load_ws_module()
    monkeypatch.setenv("WS_READ_TIMEOUT", "not-a-number")
    assert ws._read_timeout_from_env() == ws.DEFAULT_WS_READ_TIMEOUT_S
    monkeypatch.setenv("WS_READ_TIMEOUT", "12.5")
    assert ws._read_timeout_from_env() == 12.5
    monkeypatch.delenv("WS_READ_TIMEOUT", raising=False)
    assert ws._read_timeout_from_env() == ws.DEFAULT_WS_READ_TIMEOUT_S
