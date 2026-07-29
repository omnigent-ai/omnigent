"""Tests for scripts/mypy_gate.py baseline comparison."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/mypy_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("mypy_gate", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_mypy_output_fingerprints() -> None:
    mod = _load()
    text = (
        'omnigent/foo.py:10: error: Explicit "Any" is not allowed  [explicit-any]\n'
        "omnigent/foo.py:11: note: something\n"
        "Found 1 error in 1 file\n"
    )
    assert mod.parse_mypy_output(text) == [
        'omnigent/foo.py\texplicit-any\tExplicit "Any" is not allowed'
    ]


def test_new_errors_uses_multiset() -> None:
    mod = _load()
    baseline = ["a\tx\tm", "a\tx\tm"]
    current = ["a\tx\tm", "a\tx\tm", "a\tx\tm"]
    assert mod.new_errors(current, baseline) == ["a\tx\tm"]
    assert mod.new_errors(baseline, current) == []


def test_resolved_errors_do_not_fail() -> None:
    mod = _load()
    baseline = ["old\tx\tm", "keep\ty\tn"]
    current = ["keep\ty\tn"]
    assert mod.new_errors(current, baseline) == []


def test_format_stale_warning_includes_count_and_sample() -> None:
    mod = _load()
    stale = [f"omnigent/a.py\tcode\tmsg-{i}" for i in range(15)]
    text = mod.format_stale_warning(stale, sample=3)
    assert "15 baseline fingerprint(s)" in text
    assert "--write-baseline" in text
    assert "msg-0" in text
    assert "msg-2" in text
    assert "msg-3" not in text  # sample capped at 3


def test_mypy_crash_exit_is_nonzero(tmp_path: Path, capsys) -> None:
    """A mypy invocation failure must never look like a green gate."""
    mod = _load()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("omnigent/x.py\tmisc\tobd\n", encoding="utf-8")

    with mock.patch.object(mod, "run_mypy", return_value=(2, [], "mypy: boom\n")):
        code = mod.main(["--baseline", str(baseline)])
    assert code == 2
    err = capsys.readouterr().err
    assert "refuses to pass" in err


def test_mypy_spawn_oserror_is_nonzero(tmp_path: Path) -> None:
    mod = _load()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("omnigent/x.py\tmisc\tobd\n", encoding="utf-8")

    with mock.patch.object(
        mod,
        "run_mypy",
        return_value=(127, [], "failed to invoke mypy: No such file"),
    ):
        assert mod.main(["--baseline", str(baseline)]) == 127


def test_stale_baseline_warns_but_passes(tmp_path: Path, capsys) -> None:
    """Fixed-but-unpruned baseline entries warn; they do not fail the gate."""
    mod = _load()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        'omnigent/fixed.py\texplicit-any\tExplicit "Any" is not allowed\n'
        "omnigent/still.py\tmisc\tstill broken\n",
        encoding="utf-8",
    )
    current = ["omnigent/still.py\tmisc\tstill broken"]

    with mock.patch.object(mod, "run_mypy", return_value=(1, current, "")):
        code = mod.main(["--baseline", str(baseline)])
    assert code == 0
    err = capsys.readouterr().err
    assert "1 baseline fingerprint(s)" in err
    assert "omnigent/fixed.py" in err
    assert "--write-baseline" in err


def test_write_baseline_refuses_on_mypy_crash(tmp_path: Path) -> None:
    mod = _load()
    baseline = tmp_path / "baseline.txt"
    with mock.patch.object(mod, "run_mypy", return_value=(2, [], "crash")):
        code = mod.main(["--baseline", str(baseline), "--write-baseline"])
    assert code == 2
    assert not baseline.exists()
