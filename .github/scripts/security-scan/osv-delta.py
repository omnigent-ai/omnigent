#!/usr/bin/env python3
"""Audit only the uv.lock pins a PR adds or changes.

Part of the single contributor Security Scan (.github/workflows/security-scan.yml).
Auditing the PR's whole locked resolution failed every one-advisory security
bump on the advisories it did not touch, so none could merge. Instead this
reads the base and head ``uv.lock`` directly, takes the registry pins
(``name==version``) present in the head but not the base -- new packages and
changed versions -- and runs ONE pip-audit over just those. A pin the PR did
not move is not the PR's to answer for; a pin it did move must be clean.

Fail-closed: anything that stops the audit from running (unreadable lockfile,
pip-audit writing no report, a malformed report) exits 1 with a ``::error``
naming the step, never a traceback and never a silent pass. There is no
``uv export`` on either side, so a base that cannot be resolved cannot block
an innocent PR; the base lockfile is only parsed, and if it is absent the PR
is treated as introducing every pin it carries.

Usage:   osv-delta.py --base-lock <base uv.lock> --head-lock <head uv.lock> [--work <dir>]
Env:     OSV_DELTA_PIP_AUDIT  pip-audit command (default ``uvx pip-audit``)
Exit:    1 if an added/changed pin has an advisory or the audit could not run; 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import tomllib

Pin = tuple[str, str]  # (package name, version)


class AuditError(Exception):
    """The audit could not run or could not be read; the message names the step."""


def registry_pins(lock_path: Path, *, required: bool) -> set[Pin]:
    """Return the ``(name, version)`` pins of registry-sourced packages in a uv.lock.

    Editable / path / git sources are skipped: they have no PyPI release to
    audit, and pip-audit cannot resolve them from a ``name==version`` line.

    :param lock_path: The lockfile.
    :param required: ``True`` for the head (missing is an error); ``False`` for
        the base, where a missing file means the PR introduces the lockfile.
    :raises AuditError: If the file is unreadable or not a uv.lock.
    """
    if not lock_path.is_file():
        if required:
            raise AuditError(f"{lock_path} does not exist")
        return set()
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"{lock_path} is not readable TOML: {exc}") from exc
    packages = data.get("package")
    if not isinstance(packages, list):
        raise AuditError(f"{lock_path} has no [[package]] entries; not a uv.lock")
    pins: set[Pin] = set()
    for pkg in packages:
        source = pkg.get("source") if isinstance(pkg, dict) else None
        if not isinstance(source, dict) or "registry" not in source:
            continue
        name, version = pkg.get("name"), pkg.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AuditError(f"{lock_path}: package entry without name/version: {pkg!r}")
        pins.add((name.lower(), version))
    return pins


def run_pip_audit(pins: set[Pin], work: Path, command: list[str]) -> list[dict]:
    """Audit the given pins; return the pip-audit dependency records.

    pip-audit exits 1 when it finds advisories but still writes the report, so
    only a missing or unparseable report means the audit did not run.

    :raises AuditError: If pip-audit wrote no report or the report is malformed.
    """
    requirements = work / "added-pins.txt"
    requirements.write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(pins)), encoding="utf-8"
    )
    report = work / "added-pins-report.json"
    cmd = [
        *command,
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
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:]) or "(no stderr)"
        raise AuditError(f"pip-audit wrote no report (exit {proc.returncode}): {tail}")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AuditError(f"pip-audit report is not valid JSON: {exc}") from exc
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list) or not all(isinstance(d, dict) for d in deps):
        raise AuditError("pip-audit report has no 'dependencies' list of objects")
    return deps


def findings_table(deps: list[dict]) -> list[str]:
    """One line per advisory: ``name version id fix-versions``."""
    rows: list[str] = []
    for dep in deps:
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict) or "id" not in vuln:
                raise AuditError(f"malformed vuln entry for {dep.get('name')}: {vuln!r}")
            fixes = ",".join(str(f) for f in vuln.get("fix_versions") or [])
            rows.append(f"{dep.get('name')}  {dep.get('version')}  {vuln['id']}  {fixes}")
    return rows


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("### OSV audit of added uv.lock pins\n\n" + "\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-lock", required=True, type=Path)
    parser.add_argument("--head-lock", required=True, type=Path)
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args(argv)
    work = args.work or Path(tempfile.mkdtemp(prefix="osv-delta-"))
    work.mkdir(parents=True, exist_ok=True)
    command = shlex.split(os.environ.get("OSV_DELTA_PIP_AUDIT", "uvx pip-audit"))

    try:
        head = registry_pins(args.head_lock, required=True)
        base = registry_pins(args.base_lock, required=False)
        added = head - base
        if not added:
            print("uv.lock adds or changes no registry pins; nothing to audit.")
            return 0
        print(f"Auditing {len(added)} added/changed pin(s):")
        for name, version in sorted(added):
            print(f"  {name}=={version}")
        rows = findings_table(run_pip_audit(added, work, command))
    except AuditError as exc:
        print(f"::error::OSV audit could not run: {exc}")
        _summary([f"- **Audit could not run:** {exc}"])
        return 1

    if rows:
        print("\nAdvisories on pins this PR adds or changes:\nName  Version  ID  Fix Versions")
        print("\n".join(rows))
        print(
            f"::error::{len(rows)} advisory/advisories on pin(s) this PR adds or changes. "
            "Bump to a fixed version (see Fix Versions above)."
        )
        _summary([f"- **{len(rows)} advisory/advisories on added/changed pins (blocking).**"])
        return 1
    print(f"\nNo advisories on the {len(added)} added/changed pin(s).")
    _summary([f"- Audited {len(added)} added/changed pin(s); no advisories."])
    return 0


if __name__ == "__main__":
    sys.exit(main())
