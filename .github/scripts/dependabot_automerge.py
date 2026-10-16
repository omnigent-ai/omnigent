#!/usr/bin/env python3
"""Evaluate routine Dependabot upgrades using trusted policy and read-only PR data."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import zipfile
import zlib
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

from dependabot_policy import (
    NeedsReview,
    evaluate_changes,
    require,
)
from maintainer_approval import gh_json, latest_decisive_reviews, paginated

POLICY = Path(__file__).resolve().parents[1] / "dependabot-automerge.json"
REPORT_CHECKS = ("Security Scan", "Trivy CVE Scan (repository)", "Trivy CVE Scan (dev-tools)")


@cache
def git_object(repository: str, kind: str, sha: str) -> dict:
    return gh_json(["api", f"repos/{repository}/git/{kind}/{sha}"])


def read_file(repository: str, sha: str, path: str) -> str:
    parts = path.split("/")
    for index, part in enumerate(parts):
        tree = git_object(repository, "trees", sha)
        require(not tree.get("truncated"), "Git tree is incomplete")
        entries = [entry for entry in tree["tree"] if entry["path"] == part]
        require(len(entries) == 1, f"Missing file: {path}")
        entry = entries[0]
        require(
            entry["mode"] == ("100644" if index == len(parts) - 1 else "040000"),
            f"{path} must be a regular file",
        )
        sha = entry["sha"]
    blob = git_object(repository, "blobs", sha)
    require(blob["encoding"] == "base64", "Unexpected file encoding")
    return base64.b64decode(blob["content"]).decode()


def evaluate(repository: str, number: int, expected_head: str | None = None) -> dict:
    pull = gh_json(["api", f"repos/{repository}/pulls/{number}"])
    require(pull["state"] == "open" and not pull["draft"], "PR is closed or draft")
    require(
        pull["user"]["login"] == "dependabot[bot]" and pull["user"]["type"] == "Bot",
        "Not a Dependabot PR",
    )
    require(pull["head"]["repo"]["full_name"] == repository, "Fork PR requires review")
    require(pull["base"]["ref"] == "main", "Only main is covered by the pilot")
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    require(expected_head is None or head == expected_head, "PR head changed during evaluation")
    labels = {label["name"] for label in pull["labels"]}
    require(
        not labels & {"hold-automerge", "skip-security-scan"},
        "A hold or scan-waiver label is present",
    )
    files = paginated(f"repos/{repository}/pulls/{number}/files")
    require(
        pull["changed_files"] == len(files) and bool(files), "Incomplete changed-file inventory"
    )
    require(
        all(f["status"] == "modified" for f in files),
        "Added, deleted or renamed files need review",
    )
    reviews = paginated(f"repos/{repository}/pulls/{number}/reviews")
    require(
        all(r["state"] != "CHANGES_REQUESTED" for r in latest_decisive_reviews(reviews).values()),
        "A reviewer has requested changes",
    )
    policy = json.loads(POLICY.read_text())

    @cache
    def read(path):
        return read_file(repository, base, path), read_file(repository, head, path)

    decision = evaluate_changes(read, {f["filename"] for f in files}, policy)
    return dict(decision, head=head, base=base)


def require_advisory_policy(
    report: dict, packages: list[str], target="uv.lock", scanner="uv"
) -> None:
    require(isinstance(report.get("Results"), list), "Incomplete Trivy report")
    inventory = set()
    for result in report["Results"]:
        if result.get("Target") != target or result.get("Type") != scanner:
            continue
        for package in result.get("Packages", []):
            inventory.add(f"{package['Name']}=={package['Version']}")
        for vulnerability in result.get("Vulnerabilities", []):
            pin = f"{vulnerability['PkgName']}=={vulnerability['InstalledVersion']}"
            if pin in packages:
                require(
                    vulnerability["Severity"] in {"LOW", "MEDIUM"},
                    f"{pin}: {vulnerability['VulnerabilityID']} requires human review",
                )
    require(set(packages) <= inventory, "Trivy did not inventory every changed package")


def require_reports(repository: str, candidate: dict) -> None:
    head = candidate["head"]
    for name in (*REPORT_CHECKS, *candidate["checks"]):
        # Filter server-side before pagination; large CI matrices fill the first page.
        query = urlencode({"check_name": name, "per_page": 100})
        response = gh_json(["api", f"repos/{repository}/commits/{head}/check-runs?{query}"])
        require(response["total_count"] <= 100, f"Too many {name} checks to evaluate")
        runs = [run for run in response["check_runs"] if run["app"]["slug"] == "github-actions"]
        require(bool(runs), f"Waiting for {name}")
        latest = max(runs, key=lambda run: run["id"])
        require(
            latest["status"] == "completed" and latest["conclusion"] == "success",
            f"Waiting for successful {name}",
        )
    # Require actual CI completion, even if Merge Ready treats an absent
    # path-filtered check as skippable while its workflow is being created.
    completed_runs = {}
    for workflow in sorted(set(candidate["workflows"]) | {"ci.yml", "lint.yml", "trivy.yml"}):
        query = urlencode({"head_sha": head, "event": "pull_request", "per_page": 100})
        response = gh_json(
            ["api", f"repos/{repository}/actions/workflows/{workflow}/runs?{query}"]
        )
        runs = response["workflow_runs"]
        require(bool(runs), f"No {workflow} run for this head")
        latest = max(runs, key=lambda run: run["id"])
        require(
            latest["head_sha"] == head
            and latest["status"] == "completed"
            and latest["conclusion"] == "success",
            f"Latest {workflow} run did not succeed",
        )
        completed_runs[workflow] = latest
    if not candidate["target"]:
        ecosystem = "actions" if candidate["ecosystem"] == "github-actions" else "pip"
        for pin in candidate["packages"]:
            name, value = pin.split("==")
            name = (
                "/".join(name.split("/")[:2]) if ecosystem == "actions" else name.rsplit("/", 1)[1]
            )
            query = urlencode(
                {"ecosystem": ecosystem, "affects": f"{name}@{value.removeprefix('v')}"}
            )
            for advisory in paginated(f"advisories?{query}"):
                require(
                    advisory.get("withdrawn_at") or advisory["severity"] in {"low", "medium"},
                    f"{pin}: {advisory['ghsa_id']} requires human review",
                )
        return
    trivy_run = completed_runs["trivy.yml"]
    artifacts = gh_json(
        ["api", f"repos/{repository}/actions/runs/{trivy_run['id']}/artifacts?per_page=100"]
    )
    require(artifacts["total_count"] <= 100, "Trivy artifact listing is incomplete")
    reports = [
        a
        for a in artifacts["artifacts"]
        if a["name"] == "trivy-cve-report-repository" and not a["expired"]
    ]
    require(len(reports) == 1, "Missing or ambiguous Trivy repository report")
    data = subprocess.run(
        ["gh", "api", f"repos/{repository}/actions/artifacts/{reports[0]['id']}/zip"],
        check=True,
        capture_output=True,
    ).stdout
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require_advisory_policy(
                json.loads(archive.read("trivy-results.json")),
                candidate["packages"],
                candidate["target"],
                candidate["scanner"],
            )
    except (zipfile.BadZipFile, zlib.error) as exc:
        raise NeedsReview(f"Trivy repository report is unreadable: {exc}") from exc


def sweep(repository: str, mode: str) -> None:
    if mode == "merge":
        require(
            bool(os.environ.get("DEPENDABOT_MERGE_TOKEN")),
            "Merge mode requires a GitHub App token",
        )
    pulls = gh_json(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--author",
            "app/dependabot",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number",
        ]
    )
    require(len(pulls) < 100, "Dependabot PR listing reached its limit; refusing a partial sweep")
    rows = ["## Dependabot auto-merge", "", f"Mode: `{mode}`", ""]
    for pull in pulls:
        number = pull["number"]
        try:
            decision = evaluate(repository, number)
            packages = ", ".join(decision["packages"])
            rows.append(f"- #{number}: eligible ({packages})")
            require_reports(repository, decision)
            if mode == "merge":
                current = gh_json(["api", f"repos/{repository}/pulls/{number}"])
                require(
                    current["head"]["sha"] == decision["head"]
                    and current["base"]["sha"] == decision["base"],
                    "PR changed during evaluation",
                )
                require(
                    not current["draft"] and current["state"] == "open",
                    "PR is now closed or draft",
                )
                require(
                    not {label["name"] for label in current["labels"]}
                    & {"hold-automerge", "skip-security-scan"},
                    "PR was put on hold",
                )
                reviews = paginated(f"repos/{repository}/pulls/{number}/reviews")
                require(
                    all(
                        r["state"] != "CHANGES_REQUESTED"
                        for r in latest_decisive_reviews(reviews).values()
                    ),
                    "A reviewer has requested changes",
                )
                # No --admin or queued --auto: GitHub enforces required checks
                # now, against precisely the head whose policy/report we read.
                subprocess.run(
                    [
                        "gh",
                        "pr",
                        "merge",
                        str(number),
                        "--repo",
                        repository,
                        "--squash",
                        "--match-head-commit",
                        decision["head"],
                    ],
                    check=True,
                    env=dict(os.environ, GH_TOKEN=os.environ["DEPENDABOT_MERGE_TOKEN"]),
                )
                rows.append("  - Merged after policy and required checks passed.")
        except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            rows.append(f"- #{number}: human review or retry required: {exc}")
    summary = "\n".join(rows) + "\n"
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as output:
            output.write(summary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--expected-head")
    args = parser.parse_args()
    mode = os.environ.get("DEPENDABOT_AUTOMERGE_MODE", "report") or "report"
    require(mode in {"report", "merge"}, "Unknown Dependabot auto-merge mode")
    if args.pr_number is None:
        sweep(args.repository, mode)
        return 0
    try:
        decision = evaluate(args.repository, args.pr_number, args.expected_head)
        print(json.dumps(decision))
        eligible = "true"
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Human review required: {exc}")
        eligible = "false"
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as output:
            output.write(f"eligible={eligible}\n")
            if eligible == "true" and decision["ecosystem"] == "github-actions":
                output.write("workflow_pins=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
