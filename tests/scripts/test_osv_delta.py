"""Tests for the OSV advisory delta judge in the Security Scan.

The script shells out to ``uv export`` and ``pip-audit``; both are replaced
here by small stub executables so every branch of the workflow path (export
failure, missing report, malformed report, the delta verdicts) runs end to
end through the real script without network or a real lockfile.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/security-scan/osv-delta.py"

# Stub `uv`: `uv export ...` prints a few requirement lines (one editable, to
# prove it is filtered) unless the checkout contains `.export-fails`.
_UV_STUB = """\
import pathlib, sys
if pathlib.Path(".export-fails").exists():
    sys.stderr.write("error: Failed to parse `uv.lock`\\n")
    sys.exit(2)
print("-e .")
print("urllib3==2.7.0")
print("pyjwt==2.13.0")
"""

# Stub `pip-audit`: copies <reports dir>/<requirements stem>.json to --output.
# A missing fixture means "pip-audit crashed before writing a report".
_PIP_AUDIT_STUB = """\
import os, pathlib, shutil, sys
args = sys.argv[1:]
req = pathlib.Path(args[args.index("--requirement") + 1])
out = pathlib.Path(args[args.index("--output") + 1])
assert "--no-deps" in args and "json" in args
assert "-e ." not in req.read_text(), "editable requirement leaked into the audit"
fixture = pathlib.Path(os.environ["OSV_STUB_REPORTS"]) / (req.stem + ".json")
if not fixture.exists():
    sys.stderr.write("ERROR: resolution failed\\n")
    sys.exit(2)
shutil.copy(fixture, out)
sys.exit(1 if "vulns" in fixture.read_text() else 0)
"""

_OPEN = [
    ("urllib3", "2.7.0", [("PYSEC-2026-4177", ["2.8.0"])]),
    ("pyjwt", "2.13.0", [("PYSEC-2026-4145", ["2.14.0"]), ("CVE-2026-102275", ["2.15.0"])]),
]
_CLEAN: list[Any] = []
_WERKZEUG = ("werkzeug", "3.1.8", [("CVE-2026-102598", ["3.1.9"])])


def _report_json(deps: list[tuple[str, str, list[tuple[str, list[str]]]]]) -> str:
    return json.dumps(
        {
            "dependencies": [
                {
                    "name": name,
                    "version": version,
                    "vulns": [
                        {"id": vid, "fix_versions": fixes, "aliases": []} for vid, fixes in vulns
                    ],
                }
                for name, version, vulns in deps
            ],
            "fixes": [],
        }
    )


class Harness:
    """Two fake checkouts plus stub tools; ``run`` executes the real script."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path / "base"
        self.head = tmp_path / "pr"
        self.reports = tmp_path / "reports"
        self.work = tmp_path / "work"
        self.summary = tmp_path / "summary.md"
        for d in (self.base, self.head, self.reports, self.work):
            d.mkdir()
        (tmp_path / "uv_stub.py").write_text(_UV_STUB)
        (tmp_path / "pip_audit_stub.py").write_text(_PIP_AUDIT_STUB)
        self.env = {
            **os.environ,
            "OSV_DELTA_UV": f"{sys.executable} {tmp_path / 'uv_stub.py'}",
            "OSV_DELTA_PIP_AUDIT": f"{sys.executable} {tmp_path / 'pip_audit_stub.py'}",
            "OSV_STUB_REPORTS": str(self.reports),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }

    def reports_for(self, side: str, deps: list[Any] | None, *, raw: str | None = None) -> None:
        """Provide the two per-set reports for ``side`` ('base' or 'pr').

        ``deps=None`` provides nothing (pip-audit "crashes"); ``raw`` writes
        the given text verbatim instead of a well-formed report.
        """
        for name in ("main", "antigravity"):
            path = self.reports / f"{side}-{name}-req.json"
            if raw is not None:
                path.write_text(raw)
            elif deps is not None:
                # Put all findings in the main set; the antigravity set is clean.
                path.write_text(_report_json(deps if name == "main" else []))

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--base",
                str(self.base),
                "--head",
                str(self.head),
                "--work",
                str(self.work),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_single_advisory_bump_passes_while_others_remain_open(harness: Harness) -> None:
    """The deadlock case: fixing urllib3 must pass even though pyjwt is still open."""
    harness.reports_for("base", _OPEN)
    harness.reports_for("pr", _OPEN[1:])  # urllib3 fixed, pyjwt untouched

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Resolved by this PR (1)" in result.stdout
    assert "::warning::2 advisory/advisories remain open" in result.stdout
    assert "::error::" not in result.stdout
    assert "Resolved 1" in harness.summary.read_text()


def test_introducing_a_vulnerable_pin_fails(harness: Harness) -> None:
    harness.reports_for("base", _OPEN)
    harness.reports_for("pr", [*_OPEN, _WERKZEUG])

    result = harness.run()

    assert result.returncode == 1
    assert "INTRODUCED by this PR (1)" in result.stdout
    assert "werkzeug" in result.stdout
    assert "::error::This PR pins 1 package version(s)" in result.stdout


def test_new_advisory_on_an_existing_package_counts_as_introduced(harness: Harness) -> None:
    """Same package, different advisory id: the base never had it, so it blocks."""
    harness.reports_for("base", [("pyjwt", "2.13.0", [("PYSEC-2026-4145", [])])])
    harness.reports_for("pr", [("pyjwt", "2.12.0", [("PYSEC-2026-0001", [])])])

    result = harness.run()

    assert result.returncode == 1
    assert "PYSEC-2026-0001" in result.stdout


def test_clean_head_and_base_pass_quietly(harness: Harness) -> None:
    harness.reports_for("base", _CLEAN)
    harness.reports_for("pr", _CLEAN)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new advisories introduced" in result.stdout
    assert not harness.summary.exists()


def test_package_names_compare_case_insensitively(harness: Harness) -> None:
    harness.reports_for("base", [("PyJWT", "2.13.0", [("PYSEC-2026-4145", [])])])
    harness.reports_for("pr", [("pyjwt", "2.13.0", [("PYSEC-2026-4145", [])])])

    assert harness.run().returncode == 0


# ── The base side failing must not block an innocent PR ─────────────


def test_base_export_failure_warns_and_passes(harness: Harness) -> None:
    """main's lockfile not exporting is not the PR's fault: warn, list, pass."""
    (harness.base / ".export-fails").touch()
    harness.reports_for("pr", _OPEN)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Could not audit the base branch's uv.lock" in result.stdout
    assert "Failed to parse `uv.lock`" in result.stdout
    assert "::warning::3 advisory/advisories are open on this PR's uv.lock" in result.stdout
    assert "::error::" not in result.stdout
    assert "Baseline unavailable" in harness.summary.read_text()


def test_base_pip_audit_crash_warns_and_passes(harness: Harness) -> None:
    harness.reports_for("base", None)  # no fixture → stub pip-audit exits 2, writes nothing
    harness.reports_for("pr", _CLEAN)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pip-audit wrote no report" in result.stdout
    assert "No advisories on the PR head (baseline unavailable" in result.stdout


# ── The head side failing is the PR's problem and must fail loudly ──


def test_head_export_failure_fails(harness: Harness) -> None:
    (harness.head / ".export-fails").touch()
    harness.reports_for("base", _CLEAN)

    result = harness.run()

    assert result.returncode == 1
    assert "::error::Could not audit the PR head's uv.lock" in result.stdout
    assert "uv export (main set) failed" in result.stdout


def test_head_missing_report_fails(harness: Harness) -> None:
    harness.reports_for("base", _CLEAN)
    harness.reports_for("pr", None)

    result = harness.run()

    assert result.returncode == 1
    assert "pip-audit wrote no report for pr-main-req.txt" in result.stdout


# ── Malformed reports are a clean error, never a traceback ──────────


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("{not json", "is not valid JSON"),
        ('{"fixes": []}', "no 'dependencies' list"),
        ('{"dependencies": ["urllib3"]}', "dependency entry is not an object"),
        ('{"dependencies": [{"name": "urllib3", "vulns": [{"no_id": 1}]}]}', "malformed vuln"),
    ],
    ids=["not-json", "no-dependencies", "entry-not-object", "vuln-without-id"],
)
def test_malformed_head_report_is_a_clean_error(harness: Harness, raw: str, why: str) -> None:
    harness.reports_for("base", _CLEAN)
    harness.reports_for("pr", None, raw=raw)

    result = harness.run()

    assert result.returncode == 1
    assert "::error::Could not audit the PR head's uv.lock" in result.stdout
    assert why in result.stdout
    assert "Traceback" not in result.stderr


def test_malformed_base_report_counts_as_no_baseline(harness: Harness) -> None:
    harness.reports_for("base", None, raw="{not json")
    harness.reports_for("pr", _CLEAN)

    result = harness.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::Could not audit the base branch's uv.lock" in result.stdout
    assert "Traceback" not in result.stderr
