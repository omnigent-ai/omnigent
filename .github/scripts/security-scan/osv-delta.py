#!/usr/bin/env python3
"""Judge a PR's uv.lock by the advisories it INTRODUCES, not by every open one.

Part of the single contributor Security Scan (.github/workflows/security-scan.yml).
Given two checkouts -- the PR head and its base commit -- this exports each
side's full locked resolution, runs pip-audit over it, and diffs the two
reports. A PR fails only when the head carries a (package, advisory) pair the
base does not: it pinned a vulnerable version that main did not already have.
Advisories already open on the base are reported as warnings so they stay
visible, but they are not the PR's to fix. Otherwise a one-advisory security
bump could never merge while the other open advisories still sit in the
lockfile, and every such PR would deadlock against the ones it did not touch.

Failure handling is asymmetric on purpose:
  - The HEAD side failing (uv export error, pip-audit crash, unreadable report)
    is the PR's own lockfile or the audit tooling breaking -> ``::error``, exit 1.
  - The BASE side failing is not the PR's fault (main's lockfile or a tool
    drift that every PR would hit) -> ``::warning``; the head's findings are
    listed as warnings and the step passes rather than blocking an innocent
    PR on a baseline it cannot be compared against.

Usage:   osv-delta.py --base <checkout dir> --head <checkout dir> [--work <dir>]
Env:     OSV_DELTA_UV         uv command (default ``uv``)
         OSV_DELTA_PIP_AUDIT  pip-audit command (default ``uvx pip-audit``)
Exit:    1 if the head introduces an advisory the base lacks, or the head
         cannot be audited; 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

Finding = tuple[str, str]  # (package name, advisory id)
Findings = dict[Finding, tuple[str, list[str]]]  # -> (pinned version, fix versions)

# The project declares mutually exclusive extras (antigravity's protobuf>=7
# conflicts with cwsandbox/modal/databricks and the lint group), so one
# ``--all-extras`` export exits 2 before pip-audit ever runs. Export the two
# compatible resolution sets instead: everything except antigravity (with
# every dependency group, since default-groups is empty), then antigravity
# plus the ``all`` extra (which selects the locked databricks-sdk fork).
# Together they cover every registry package/version tuple in uv.lock.
_EXPORT_SETS: dict[str, list[str]] = {
    "main": ["--all-extras", "--no-extra", "antigravity", "--all-groups"],
    "antigravity": ["--extra", "antigravity", "--extra", "all", "--no-default-groups"],
}


class AuditUnavailable(Exception):
    """One side could not be audited; the message says which step broke and why."""


@dataclass
class Tools:
    uv: list[str] = field(
        default_factory=lambda: shlex.split(os.environ.get("OSV_DELTA_UV", "uv"))
    )
    pip_audit: list[str] = field(
        default_factory=lambda: shlex.split(os.environ.get("OSV_DELTA_PIP_AUDIT", "uvx pip-audit"))
    )


def _tail(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def export_requirements(checkout: Path, name: str, work: Path, tools: Tools) -> Path:
    """Export one resolution set of ``checkout``'s uv.lock as a requirements file.

    Editable local packages (the project itself + sdks/*) are dropped: pip-audit
    cannot hash an editable path requirement and errors out when one is present,
    and OSV has no advisories for local source anyway.

    :raises AuditUnavailable: If ``uv export`` fails.
    """
    out = work / f"{checkout.name}-{name}-req.txt"
    cmd = [*tools.uv, "export", "--frozen", "--format", "requirements-txt", *_EXPORT_SETS[name]]
    proc = subprocess.run(cmd, cwd=checkout, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AuditUnavailable(
            f"uv export ({name} set) failed in {checkout} with exit {proc.returncode}: "
            f"{_tail(proc.stderr) or '(no stderr)'}"
        )
    kept = [line for line in proc.stdout.splitlines() if not line.startswith("-e ")]
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return out


def run_pip_audit(requirements: Path, work: Path, tools: Tools) -> Path:
    """Audit one requirements file into a JSON report.

    pip-audit exits 1 when it finds advisories but still writes the report, so
    only a missing or empty report means the audit itself did not run.

    :raises AuditUnavailable: If pip-audit wrote no report.
    """
    report = work / (requirements.stem + "-report.json")
    cmd = [
        *tools.pip_audit,
        "--requirement",
        str(requirements),
        "--no-deps",
        "--format",
        "json",
        "--output",
        str(report),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if not report.is_file() or report.stat().st_size == 0:
        raise AuditUnavailable(
            f"pip-audit wrote no report for {requirements.name} (exit {proc.returncode}): "
            f"{_tail(proc.stderr) or '(no stderr)'}"
        )
    return report


def load_report(path: Path) -> Findings:
    """Parse a pip-audit ``--format json`` report into findings.

    :raises AuditUnavailable: If the file is not a well-formed pip-audit report.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditUnavailable(f"{path.name} is not valid JSON: {exc}") from exc
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        raise AuditUnavailable(f"{path.name} is not a pip-audit report (no 'dependencies' list)")
    findings: Findings = {}
    for dep in deps:
        if not isinstance(dep, dict):
            raise AuditUnavailable(f"{path.name}: dependency entry is not an object: {dep!r}")
        name = str(dep.get("name", "")).lower()
        version = str(dep.get("version", ""))
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict) or "id" not in vuln:
                raise AuditUnavailable(f"{path.name}: malformed vuln entry for {name}: {vuln!r}")
            fixes = vuln.get("fix_versions") or []
            findings[(name, str(vuln["id"]))] = (version, [str(f) for f in fixes])
    return findings


def audit_checkout(checkout: Path, work: Path, tools: Tools) -> Findings:
    """Export, audit and parse both resolution sets of one checkout."""
    findings: Findings = {}
    for name in _EXPORT_SETS:
        requirements = export_requirements(checkout, name, work, tools)
        findings.update(load_report(run_pip_audit(requirements, work, tools)))
    return findings


def _table(findings: Findings) -> str:
    rows = sorted(findings.items())
    width = max((len(name) for (name, _), _ in rows), default=4)
    lines = [f"{'Name':<{width}}  Version  ID                 Fix Versions"]
    for (name, vuln_id), (version, fixes) in rows:
        lines.append(f"{name:<{width}}  {version:<8} {vuln_id:<18} {','.join(fixes)}")
    return "\n".join(lines)


def _summarize(lines: list[str]) -> None:
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and lines:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("### OSV advisory delta\n\n" + "\n".join(lines) + "\n")


def judge(head: Findings, base: Findings | None) -> int:
    """Print the verdict and return the exit code.

    :param head: The PR head's findings.
    :param base: The base branch's findings, or ``None`` when the baseline
        could not be audited.
    """
    summary: list[str] = []
    if base is None:
        if head:
            print(f"Open advisories on the PR head ({len(head)}):\n{_table(head)}\n")
            print(
                f"::warning::{len(head)} advisory/advisories are open on this PR's uv.lock, "
                "but the base branch could not be audited, so they cannot be attributed to "
                "this PR and do not block it."
            )
            summary.append(
                f"- Baseline unavailable; {len(head)} open advisory/advisories reported "
                "without blocking."
            )
        else:
            print("No advisories on the PR head (baseline unavailable, nothing to attribute).")
        _summarize(summary)
        return 0

    introduced = {k: v for k, v in head.items() if k not in base}
    resolved = {k: v for k, v in base.items() if k not in head}
    carried = {k: v for k, v in head.items() if k in base}

    if resolved:
        print(f"Resolved by this PR ({len(resolved)}):\n{_table(resolved)}\n")
        summary.append(f"- Resolved {len(resolved)} advisory/advisories.")
    if carried:
        print(f"Already open on the base branch, not this PR's to fix ({len(carried)}):")
        print(_table(carried) + "\n")
        print(
            f"::warning::{len(carried)} advisory/advisories remain open on the base "
            "branch's uv.lock; they do not block this PR."
        )
        summary.append(f"- {len(carried)} advisory/advisories still open on the base branch.")
    if introduced:
        print(f"INTRODUCED by this PR ({len(introduced)}):\n{_table(introduced)}\n")
        print(
            f"::error::This PR pins {len(introduced)} package version(s) with advisories the "
            "base branch does not have. Bump to a fixed version (see Fix Versions above)."
        )
        summary.append(f"- **Introduced {len(introduced)} new advisory/advisories (blocking).**")
    else:
        print("No new advisories introduced by this PR.")
    _summarize(summary)
    return 1 if introduced else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, type=Path, help="checkout of the base commit")
    parser.add_argument("--head", required=True, type=Path, help="checkout of the PR head")
    parser.add_argument("--work", type=Path, default=None, help="scratch dir for exports/reports")
    args = parser.parse_args(argv)
    tools = Tools()
    work = args.work or Path(tempfile.mkdtemp(prefix="osv-delta-"))
    work.mkdir(parents=True, exist_ok=True)

    try:
        head = audit_checkout(args.head.resolve(), work, tools)
    except AuditUnavailable as exc:
        print(f"::error::Could not audit the PR head's uv.lock: {exc}")
        return 1

    base: Findings | None
    try:
        base = audit_checkout(args.base.resolve(), work, tools)
    except AuditUnavailable as exc:
        print(
            f"::warning::Could not audit the base branch's uv.lock, so there is no baseline: {exc}"
        )
        base = None

    return judge(head, base)


if __name__ == "__main__":
    sys.exit(main())
