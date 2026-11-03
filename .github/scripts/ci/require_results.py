#!/usr/bin/env python3
"""Fail a workflow when any selected consumer job did not actually succeed."""

from __future__ import annotations

import json
import os
import sys


def validate_results(suite: str, needs: dict, *, draft: bool) -> None:
    selection_job = "setup" if suite == "ui" else "scope"
    selection = needs[selection_job]
    if selection["result"] != "success":
        raise ValueError("Test selection did not succeed")
    outputs = selection["outputs"]
    if suite == "ci":
        if any(
            outputs[key] not in {"true", "false"}
            for key in ("backend", "database", "omnidev", "sidecar")
        ):
            raise ValueError("Invalid CI selection outputs")
        selected = dict.fromkeys(
            ("stores-postgres", "stores-cockroachdb", "stores-mysql"),
            outputs["database"] == "true",
        )
        selected["pytest"] = outputs["backend"] == "true"
        selected.update(
            {"omnidev": outputs["omnidev"] == "true", "codex-parity": outputs["sidecar"] == "true"}
        )
    elif suite == "ui":
        if outputs["ui"] not in {"full", "dependency", "sidecar", "none"}:
            raise ValueError("Invalid UI selection output")
        selected = {
            "build-sidecar": outputs["ui"] != "none",
            "e2e-ui": outputs["ui"] != "none",
            "browser-contract": outputs["ui"] in {"full", "dependency"},
        }
    elif suite in {"web", "docker"}:
        jobs = {"web-test": "web", "ios": "ios"} if suite == "web" else {"build": "docker"}
        if any(outputs[key] not in {"true", "false"} for key in jobs.values()):
            raise ValueError("Invalid consumer selection outputs")
        selected = {job: outputs[key] == "true" for job, key in jobs.items()}
    else:
        raise ValueError(f"Unknown test suite: {suite}")
    failures = []
    for job, required in selected.items():
        result = needs[job]["result"]
        allowed = {"success"} if required and not draft else {"success", "skipped"}
        print(f"{job}: {result} (selected={required and not draft})")
        if result not in allowed:
            failures.append(f"{job}: {result}")
    if failures:
        raise ValueError("Required consumer jobs did not succeed: " + ", ".join(failures))


def main() -> None:
    try:
        validate_results(
            sys.argv[1],
            json.loads(os.environ["NEEDS"]),
            draft=os.environ.get("IS_DRAFT") == "true",
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        print(f"::error::{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
