#!/usr/bin/env python3
"""Select stale UI-preview Databricks apps so their app slots can be reclaimed.

The UI Preview workflow deploys one Databricks app per labelled PR
(``omnigent-ui-preview-pr-<N>``) into a shared workspace with a hard cap on
the number of apps. The close-event cleanup deletes an app when its PR
closes, but that event is delivered exactly once: a missed or skipped delete
leaks the app slot forever, and an open PR that dropped the ``ui-preview``
label keeps holding a slot. Leaked slots accumulate until the workspace hits
its cap and every new preview deploy fails.

This script is the close-event-independent reclamation path. It lists the
workspace's apps and prints, one per line on stdout, the per-PR preview apps
whose backing PR is gone, closed, or no longer labelled ``ui-preview``.
Deleting is left to the calling workflow so each reclaimed app shows up in
the job log.

An app is selected only on positive evidence that its PR no longer needs a
preview. A PR lookup failure keeps the app, is reported on stderr, and turns
the exit code nonzero so the sweep run is visibly red instead of silently
under-reclaiming.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable

PREVIEW_APP_NAME = re.compile(r"^omnigent-ui-preview-pr-([0-9]+)$")
PREVIEW_LABEL = "ui-preview"

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def preview_pr_number(app_name: str) -> int | None:
    """The PR number a per-PR preview app belongs to, or None for other apps."""
    match = PREVIEW_APP_NAME.match(app_name)
    return int(match.group(1)) if match else None


def list_apps(run: Runner = run_command) -> list[dict]:
    """All apps in the workspace, from ``databricks apps list``."""
    proc = run(["databricks", "apps", "list", "-o", "json"])
    if proc.returncode != 0:
        raise RuntimeError(f"databricks apps list failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "[]")
    if isinstance(data, dict):
        data = data.get("apps") or []
    return [app for app in data if isinstance(app, dict)]


def pr_needs_preview(repo: str, number: int, run: Runner = run_command) -> bool:
    """Whether the PR still needs its preview app.

    False only on positive evidence: the PR is gone (404), closed/merged, or
    open without the ``ui-preview`` label. Raises RuntimeError on any other
    lookup failure so the caller keeps the app and surfaces the error.
    """
    proc = run(["gh", "api", f"repos/{repo}/pulls/{number}"])
    if proc.returncode != 0:
        if "HTTP 404" in proc.stderr:
            return False
        raise RuntimeError(f"PR #{number} lookup failed: {proc.stderr.strip()}")
    pr = json.loads(proc.stdout)
    if pr.get("state") != "open":
        return False
    labels = {label.get("name") for label in pr.get("labels") or []}
    return PREVIEW_LABEL in labels


def select_stale_apps(
    repo: str, apps: list[dict], run: Runner = run_command
) -> tuple[list[str], list[str]]:
    """Split the workspace's per-PR preview apps into (stale names, errors)."""
    stale: list[str] = []
    errors: list[str] = []
    for app in apps:
        name = str(app.get("name") or "")
        number = preview_pr_number(name)
        if number is None:
            continue  # not a per-PR preview app (e.g. omnigent-ui-preview-dev)
        try:
            if not pr_needs_preview(repo, number, run=run):
                stale.append(name)
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
    return stale, errors


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    try:
        apps = list_apps()
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    stale, errors = select_stale_apps(repo, apps)
    for error in errors:
        print(f"::warning::{error} -- keeping its app this sweep", file=sys.stderr)
    preview_total = sum(preview_pr_number(str(a.get("name") or "")) is not None for a in apps)
    print(
        f"{len(apps)} apps in workspace, {preview_total} per-PR preview apps, "
        f"{len(stale)} stale, {len(errors)} lookup failures",
        file=sys.stderr,
    )
    for name in stale:
        print(name)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
