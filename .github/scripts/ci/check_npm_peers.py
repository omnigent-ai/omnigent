#!/usr/bin/env python3
"""Validate peers affected by a lockfile update without re-resolving dependencies."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml


def changed_peer_checks(before: dict, after: dict) -> list[dict]:
    if any(str(lock.get("lockfileVersion")) != "9.0" for lock in (before, after)):
        raise ValueError("Peer validation requires pnpm lockfile version 9.0")
    checks = []
    for key, snapshot in after["snapshots"].items():
        package = key.split("(", 1)[0]
        metadata = after["packages"][package]
        if (
            before["snapshots"].get(key) == snapshot
            and before["packages"].get(package) == metadata
        ):
            continue
        dependencies = snapshot.get("dependencies", {}) | snapshot.get("optionalDependencies", {})
        for name, wanted in metadata.get("peerDependencies", {}).items():
            resolved = dependencies.get(name)
            optional = (
                metadata.get("peerDependenciesMeta", {}).get(name, {}).get("optional", False)
            )
            if resolved is None and optional:
                continue
            pin = str(resolved).split("(", 1)[0].rsplit("@", 1)[-1] if resolved else ""
            checks.append({"package": key, "peer": name, "wanted": wanted, "resolved": pin})
    return checks


def validate_peers(checks: list[dict]) -> None:
    # Node's npm distribution carries the same semver evaluator npm uses.
    npm_root = subprocess.check_output(["npm", "root", "-g"], text=True).strip()
    semver = Path(npm_root) / "npm/node_modules/semver"
    if not (semver / "package.json").is_file():
        raise ValueError(f"npm peer validation requires npm's semver package at {semver}")
    process = subprocess.run(
        [
            "node",
            "-e",
            "const fs = require('node:fs'); const semver = require(process.argv[1]); "
            "const checks = JSON.parse(fs.readFileSync(0, 'utf8')); "
            "process.stdout.write(JSON.stringify(checks.filter(c => "
            "!semver.valid(c.resolved) || !semver.validRange(c.wanted) || "
            "!semver.satisfies(c.resolved, c.wanted, {includePrerelease: true}))));",
            str(semver),
        ],
        input=json.dumps(checks),
        text=True,
        capture_output=True,
        check=True,
    )
    failures = json.loads(process.stdout)
    if failures:
        raise ValueError("Incompatible changed npm peers:\n" + json.dumps(failures, indent=2))
    print(f"Validated {len(checks)} peer requirements in changed npm resolutions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    locks = [
        yaml.safe_load(
            subprocess.check_output(["git", "show", f"{revision}:pnpm-lock.yaml"], text=True)
        )
        for revision in (args.base, args.head)
    ]
    validate_peers(changed_peer_checks(*locks))


if __name__ == "__main__":
    main()
