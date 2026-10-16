#!/usr/bin/env python3
"""Evaluate routine Dependabot upgrades using trusted policy and read-only PR data."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import io
import json
import os
import re
import subprocess
import zipfile
import zlib
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

from dependabot_policy import (
    NeedsReview,
    action_changes,
    evaluate_changes,
    require,
)
from maintainer_approval import gh_json, latest_decisive_reviews, paginated

POLICY = Path(__file__).resolve().parents[1] / "dependabot-automerge.json"
REPORT_CHECKS = (
    "Security Scan",
    "Trivy CVE Scan (repository)",
    "Trivy CVE Scan (dev-tools)",
)


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


def evaluate(
    repository: str, number: int, expected_head: str | None = None, *, for_scan: bool = False
) -> dict:
    pull = gh_json(["api", f"repos/{repository}/pulls/{number}"])
    require(pull["state"] == "open" and not pull["draft"], "PR is closed or draft")
    require(
        pull["user"]["login"] == "dependabot[bot]" and pull["user"]["type"] == "Bot",
        "Not a Dependabot PR",
    )
    require(pull["head"]["repo"]["full_name"] == repository, "Fork PR requires review")
    require(pull["base"]["ref"] == "main", "Only main is covered by auto-merge")
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    require(expected_head is None or head == expected_head, "PR head changed during evaluation")
    labels = {label["name"] for label in pull["labels"]}
    require(
        for_scan or not labels & {"hold-automerge", "skip-security-scan"},
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
        for_scan
        or all(
            r["state"] != "CHANGES_REQUESTED" for r in latest_decisive_reviews(reviews).values()
        ),
        "A reviewer has requested changes",
    )
    policy = json.loads(POLICY.read_text())
    comparison = gh_json(["api", f"repos/{repository}/compare/{base}...{head}"])
    merge_base = comparison["merge_base_commit"]["sha"]

    @cache
    def read(path):
        return read_file(repository, merge_base, path), read_file(repository, head, path)

    paths = {f["filename"] for f in files}
    decision = (
        action_changes(read, paths, policy, for_scan=True)
        if for_scan
        else evaluate_changes(read, paths, policy)
    )
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


def latest_check(repository: str, head: str, name: str) -> dict:
    # Filter server-side; large CI matrices fill the first page.
    query = urlencode({"check_name": name, "per_page": 100})
    response = gh_json(["api", f"repos/{repository}/commits/{head}/check-runs?{query}"])
    require(response["total_count"] <= 100, f"Too many {name} checks to evaluate")
    runs = [run for run in response["check_runs"] if run["app"]["slug"] == "github-actions"]
    require(bool(runs), f"Waiting for {name}")
    return max(runs, key=lambda run: run["id"])


def require_approval(repository: str, candidate: dict, mode: str) -> None:
    if mode != "merge":
        return
    check = latest_check(repository, candidate["head"], "Maintainer Approval")
    require(check["status"] == "completed", "Waiting for Maintainer Approval")
    if check["conclusion"] == "success":
        return
    require(check["conclusion"] == "failure", "Maintainer Approval needs human intervention")
    match = re.fullmatch(
        rf"https://github.com/{re.escape(repository)}/actions/runs/(\d+)/job/\d+",
        check.get("details_url", ""),
    )
    require(match is not None, "Cannot identify Maintainer Approval workflow run")
    run_id = match[1]
    run = gh_json(["api", f"repos/{repository}/actions/runs/{run_id}"])
    require(
        run["path"] == ".github/workflows/maintainer-approval.yml"
        and run["event"] == "pull_request_target"
        and run["status"] == "completed"
        and run["conclusion"] == "failure",
        "Approval workflow cannot be refreshed",
    )
    require(run["run_attempt"] < 3, "Approval refresh retry limit reached; inspect the run")
    # Cooldown expiry and mode changes do not generate a new PR event.
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/actions/runs/{run_id}/rerun-failed-jobs",
        ],
        check=True,
        env=dict(os.environ, GH_TOKEN=os.environ["DEPENDABOT_MERGE_TOKEN"]),
    )
    raise NeedsReview("Refreshed Maintainer Approval; waiting for the next sweep")


def require_reports(repository: str, candidate: dict) -> None:
    head = candidate["head"]
    # Merge Ready is a commit status, unlike the workflow job check runs.
    statuses = paginated(f"repos/{repository}/commits/{head}/statuses")
    merge_ready = [status for status in statuses if status["context"] == "Merge Ready"]
    require(
        bool(merge_ready)
        and max(merge_ready, key=lambda status: status["id"])["state"] == "success",
        "Waiting for successful Merge Ready status",
    )
    for name in (*REPORT_CHECKS, *candidate["checks"]):
        latest = latest_check(repository, head, name)
        require(
            latest["status"] == "completed" and latest["conclusion"] == "success",
            f"Waiting for successful {name}",
        )
    # Require actual CI completion, even if Merge Ready treats an absent
    # path-filtered check as skippable while its workflow is being created.
    completed_runs = {}
    for workflow in sorted(set(candidate["workflows"]) | {"ci.yml", "lint.yml", "trivy.yml"}):
        latest, jobs = latest_workflow(repository, head, workflow)
        require(
            latest["head_sha"] == head
            and latest["status"] == "completed"
            and latest["conclusion"] == "success",
            f"Latest {workflow} run did not succeed",
        )
        required_jobs = json.loads(POLICY.read_text())["workflow_test_jobs"][workflow]
        for pattern in required_jobs:
            matching = [job for job in jobs if fnmatch.fnmatchcase(job["name"], pattern)]
            require(
                bool(matching) and all(job["conclusion"] == "success" for job in matching),
                f"{workflow}: waiting for successful {pattern} jobs",
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


def latest_workflow(repository: str, head: str, workflow: str) -> tuple[dict, list[dict]]:
    query = urlencode({"head_sha": head, "event": "pull_request", "per_page": 100})
    response = gh_json(["api", f"repos/{repository}/actions/workflows/{workflow}/runs?{query}"])
    for run in sorted(response["workflow_runs"], key=lambda run: run["id"], reverse=True):
        require(run["head_sha"] == head, "Workflow head does not match the candidate")
        jobs = paginated(
            f"repos/{repository}/actions/runs/{run['id']}/jobs",
            request=lambda args: gh_json(args)["jobs"],
        )
        if (
            workflow == "e2e.yml"
            and run["status"] == "completed"
            and run["conclusion"] in {"success", "skipped"}
            and jobs
            and all(job["conclusion"] == "skipped" for job in jobs)
        ):
            continue  # Unrelated label events create an entirely skipped E2E run.
        return run, jobs
    raise NeedsReview(f"No executed {workflow} run for this head")


def refresh_failed_gates(repository: str, candidate: dict, mode: str) -> None:
    if mode != "merge":
        return
    scan = latest_check(repository, candidate["head"], "Security Scan")
    require(
        scan["status"] == "completed" and scan["conclusion"] == "success",
        "Waiting for successful Security Scan",
    )
    refreshed = []
    for workflow in sorted(set(candidate["workflows"]) | {"ci.yml", "lint.yml", "trivy.yml"}):
        run, jobs = latest_workflow(repository, candidate["head"], workflow)
        if run["status"] != "completed" or run["conclusion"] != "failure":
            continue
        failed = [job for job in jobs if job["conclusion"] not in {"success", "skipped"}]
        if not failed or not all(
            job["name"].endswith("Security Gate") and job["conclusion"] == "failure"
            for job in failed
        ):
            continue
        require(run["run_attempt"] < 3, f"{workflow}: gate refresh retry limit reached")
        # Re-run all jobs: the test jobs behind the failed gate were skipped.
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repository}/actions/runs/{run['id']}/rerun",
            ],
            check=True,
            env=dict(os.environ, GH_TOKEN=os.environ["DEPENDABOT_MERGE_TOKEN"]),
        )
        refreshed.append(workflow)
    require(not refreshed, f"Refreshed blocked CI: {', '.join(refreshed)}; waiting for next sweep")


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
            require_approval(repository, decision, mode)
            refresh_failed_gates(repository, decision, mode)
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
    parser.add_argument("--check-action-pins", action="store_true")
    args = parser.parse_args()
    mode = os.environ.get("DEPENDABOT_AUTOMERGE_MODE", "report") or "report"
    require(mode in {"report", "merge"}, "Unknown Dependabot auto-merge mode")
    if args.check_action_pins and args.pr_number is None:
        parser.error("--check-action-pins requires --pr-number")
    if args.pr_number is None:
        sweep(args.repository, mode)
        return 0
    try:
        decision = evaluate(
            args.repository, args.pr_number, args.expected_head, for_scan=args.check_action_pins
        )
        print(json.dumps(decision))
        eligible = "true"
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        prefix = "No workflow-pin exception" if args.check_action_pins else "Human review required"
        print(f"{prefix}: {exc}")
        eligible = "false"
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as output:
            if not args.check_action_pins:
                output.write(f"eligible={eligible}\n")
            if args.check_action_pins:
                output.write(f"workflow_pins={eligible}\n")
            elif eligible == "true" and decision["ecosystem"] == "github-actions":
                output.write("workflow_pins=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
