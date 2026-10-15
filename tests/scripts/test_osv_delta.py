"""Tests for the added-pins OSV audit in the Security Scan.

``pip-audit`` is replaced by a stub executable, so every branch of the
workflow path -- pin extraction from both lockfiles, the single audit call,
missing/malformed reports, fail-closed errors -- runs end to end through the
real script without network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/security-scan/osv-delta.py"

# Stub `pip-audit`: records the requirements it was given, then copies the
# fixture report named by OSV_STUB_REPORT to --output (or writes nothing when
# OSV_STUB_REPORT is unset, modelling a crash before the report).
_PIP_AUDIT_STUB = """\
import os, pathlib, shutil, sys
args = sys.argv[1:]
req = pathlib.Path(args[args.index("--requirement") + 1])
out = pathlib.Path(args[args.index("--output") + 1])
assert "--no-deps" in args and "json" in args
pathlib.Path(os.environ["OSV_STUB_SEEN"]).write_text(req.read_text())
fixture = os.environ.get("OSV_STUB_REPORT")
if not fixture:
    sys.stderr.write("ERROR: resolver exploded\\n")
    sys.exit(2)
shutil.copy(fixture, out)
sys.exit(1 if "PYSEC" in pathlib.Path(fixture).read_text() else 0)
"""


def _lock(*packages: tuple[str, str, str]) -> str:
    """Build a uv.lock from ``(name, version, source)`` triples (source: registry|editable)."""
    body = ['version = 1\nrevision = 3\nrequires-python = ">=3.12"\n']
    for name, version, source in packages:
        src = (
            'source = { registry = "https://pypi.org/simple" }'
            if source == "registry"
            else 'source = { editable = "." }'
        )
        body.append(f'[[package]]\nname = "{name}"\nversion = "{version}"\n{src}\n')
    return "\n".join(body)


def _report(deps: list[tuple[str, str, list[tuple[str, list[str]]]]]) -> str:
    return json.dumps(
        {
            "dependencies": [
                {
                    "name": n,
                    "version": v,
                    "vulns": [{"id": i, "fix_versions": f, "aliases": []} for i, f in vulns],
                }
                for n, v, vulns in deps
            ],
            "fixes": [],
        }
    )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.base_lock = tmp_path / "base" / "uv.lock"
        self.head_lock = tmp_path / "pr" / "uv.lock"
        self.seen = tmp_path / "seen-requirements.txt"
        self.summary = tmp_path / "summary.md"
        for p in (self.base_lock, self.head_lock):
            p.parent.mkdir()
        stub = tmp_path / "pip_audit_stub.py"
        stub.write_text(_PIP_AUDIT_STUB)
        self.env = {
            **os.environ,
            "OSV_DELTA_PIP_AUDIT": f"{sys.executable} {stub}",
            "OSV_STUB_SEEN": str(self.seen),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        self.env.pop("OSV_STUB_REPORT", None)

    def report(self, deps: list) -> None:
        path = self.tmp / "report.json"
        path.write_text(_report(deps))
        self.env["OSV_STUB_REPORT"] = str(path)

    def raw_report(self, text: str) -> None:
        path = self.tmp / "report.json"
        path.write_text(text)
        self.env["OSV_STUB_REPORT"] = str(path)

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--base-lock",
                str(self.base_lock),
                "--head-lock",
                str(self.head_lock),
                "--work",
                str(self.tmp / "work"),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def audited(self) -> list[str]:
        return self.seen.read_text().split() if self.seen.exists() else []


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


_BASE = (
    ("urllib3", "2.7.0", "registry"),
    ("pyjwt", "2.13.0", "registry"),
    ("omnigent", "0.18.0.dev0", "editable"),
)


def test_unchanged_pins_are_not_audited_even_with_open_advisories(h: Harness) -> None:
    """The deadlock case: a PR that moves only urllib3 is judged on urllib3 alone."""
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(("urllib3", "2.8.0", "registry"), *_BASE[1:]))
    h.report([("urllib3", "2.8.0", [])])

    result = h.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert h.audited() == ["urllib3==2.8.0"]  # pyjwt 2.13.0 (still vulnerable) not audited
    assert "No advisories on the 1 added/changed pin(s)" in result.stdout
    assert "Audited 1" in h.summary.read_text()


def test_added_vulnerable_pin_fails(h: Harness) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(*_BASE, ("werkzeug", "3.1.8", "registry")))
    h.report([("werkzeug", "3.1.8", [("PYSEC-2026-1", ["3.1.9"])])])

    result = h.run()

    assert result.returncode == 1
    assert h.audited() == ["werkzeug==3.1.8"]
    assert "werkzeug  3.1.8  PYSEC-2026-1  3.1.9" in result.stdout
    assert "::error::1 advisory/advisories on pin(s) this PR adds or changes" in result.stdout


def test_downgrade_to_a_vulnerable_version_counts_as_changed(h: Harness) -> None:
    h.base_lock.write_text(_lock(("pyjwt", "2.15.1", "registry")))
    h.head_lock.write_text(_lock(("pyjwt", "2.13.0", "registry")))
    h.report([("pyjwt", "2.13.0", [("PYSEC-2026-4145", ["2.14.0"])])])

    assert h.run().returncode == 1
    assert h.audited() == ["pyjwt==2.13.0"]


def test_no_pin_changes_skips_pip_audit_entirely(h: Harness) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(*_BASE))

    result = h.run()

    assert result.returncode == 0
    assert "nothing to audit" in result.stdout
    assert h.audited() == []


def test_editable_and_path_sources_are_ignored(h: Harness) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(*_BASE[:2], ("omnigent", "0.19.0", "editable")))

    result = h.run()

    assert result.returncode == 0
    assert h.audited() == []


def test_missing_base_lock_audits_every_head_pin(h: Harness) -> None:
    """A PR introducing uv.lock has no baseline: every registry pin is its own."""
    h.head_lock.write_text(_lock(*_BASE))
    h.report([])

    result = h.run()

    assert result.returncode == 0, result.stdout
    assert h.audited() == ["pyjwt==2.13.0", "urllib3==2.7.0"]


# ── Fail-closed: anything that stops the audit is an error, never a pass ──


def test_missing_head_lock_is_an_error(h: Harness) -> None:
    h.base_lock.write_text(_lock(*_BASE))

    result = h.run()

    assert result.returncode == 1
    assert "::error::OSV audit could not run" in result.stdout
    assert "does not exist" in result.stdout


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("this is = not [[ toml", "not readable TOML"),
        ('version = 1\n[[other]]\nname = "x"\n', "no [[package]] entries"),
        ('[[package]]\nname = "x"\nsource = { registry = "r" }\n', "without name/version"),
    ],
    ids=["bad-toml", "not-a-lock", "entry-missing-version"],
)
def test_malformed_lockfile_is_a_clean_error(h: Harness, text: str, why: str) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(text)

    result = h.run()

    assert result.returncode == 1
    assert why in result.stdout
    assert "Traceback" not in result.stderr


def test_pip_audit_writing_no_report_is_an_error(h: Harness) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(*_BASE, ("werkzeug", "3.1.9", "registry")))
    # No OSV_STUB_REPORT → the stub exits 2 without writing.

    result = h.run()

    assert result.returncode == 1
    assert "pip-audit wrote no report (exit 2)" in result.stdout
    assert "resolver exploded" in result.stdout


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("{not json", "not valid JSON"),
        ('{"fixes": []}', "no 'dependencies' list"),
        ('{"dependencies": ["werkzeug"]}', "no 'dependencies' list"),
        ('{"dependencies": [{"name": "werkzeug", "vulns": [{"no_id": 1}]}]}', "malformed vuln"),
    ],
    ids=["not-json", "no-dependencies", "entry-not-object", "vuln-without-id"],
)
def test_malformed_report_is_a_clean_error(h: Harness, raw: str, why: str) -> None:
    h.base_lock.write_text(_lock(*_BASE))
    h.head_lock.write_text(_lock(*_BASE, ("werkzeug", "3.1.9", "registry")))
    h.raw_report(raw)

    result = h.run()

    assert result.returncode == 1
    assert "::error::OSV audit could not run" in result.stdout
    assert why in result.stdout
    assert "Traceback" not in result.stderr
