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
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

from dependabot_policy import (
    NeedsReview,
    action_changes,
    evaluate_changes,
    require,
    require_refresh_permissions,
)
from maintainer_approval import gh_json, latest_decisive_reviews, paginated

POLICY = Path(__file__).resolve().parents[1] / "dependabot-automerge.json"
REPORT_CHECKS = (
    "Security Scan",
    "Trivy CVE Scan (repository)",
    "Trivy CVE Scan (dev-tools)",
)


class StaleBase(NeedsReview):
    """Completed CI used a different base from current main."""


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


def main_head(repository: str) -> str:
    # A PR's base.sha can lag the live target branch.
    return gh_json(["api", f"repos/{repository}/git/ref/heads/main"])["object"]["sha"]


@cache
def refresh_workflows(repository: str, sha: str) -> dict[str, str]:
    tree_sha = sha
    for part in (".github", "workflows"):
        tree = git_object(repository, "trees", tree_sha)
        require(not tree.get("truncated"), "Workflow inventory is incomplete")
        entries = [entry for entry in tree["tree"] if entry["path"] == part]
        require(len(entries) == 1 and entries[0]["mode"] == "040000", "Missing workflow inventory")
        tree_sha = entries[0]["sha"]
    tree = git_object(repository, "trees", tree_sha)
    require(not tree.get("truncated"), "Workflow inventory is incomplete")
    paths = [entry["path"] for entry in tree["tree"] if entry["path"].endswith((".yml", ".yaml"))]
    require(bool(paths), "Missing workflow inventory")
    return {
        f".github/workflows/{path}": read_file(repository, sha, f".github/workflows/{path}")
        for path in paths
    }


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
    head, base = pull["head"]["sha"], main_head(repository)
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
    return dict(decision, number=number, head=head, base=base)


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


def approval_refresh_run(repository: str, candidate: dict) -> str | None:
    """Identify a recoverable approval failure without dispatching or consuming retries."""
    check = latest_check(repository, candidate["head"], "Maintainer Approval")
    require(check["status"] == "completed", "Waiting for Maintainer Approval")
    if check["conclusion"] == "success":
        return None
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
    return run_id


def require_approval(repository: str, candidate: dict, mode: str) -> None:
    if mode != "merge":
        return
    run_id = approval_refresh_run(repository, candidate)
    if run_id is None:
        return
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


def required_workflows(candidate: dict) -> set[str]:
    workflows = set(candidate["workflows"]) | {"ci.yml", "lint.yml", "trivy.yml"}
    if candidate["ecosystem"] in {"python", "github-actions"}:
        workflows.add("windows.yml")
    return workflows


def require_merge_ready(repository: str, candidate: dict, mode: str) -> None:
    head = candidate["head"]
    # Merge Ready is a commit status, unlike the workflow job check runs.
    statuses = paginated(f"repos/{repository}/commits/{head}/statuses")
    merge_ready = [status for status in statuses if status["context"] == "Merge Ready"]
    if merge_ready and max(merge_ready, key=lambda status: status["id"])["state"] == "success":
        return
    if mode == "merge":
        require_current_candidate(repository, candidate["number"], candidate)
        query = urlencode(
            {
                "event": "workflow_dispatch",
                "created": ">=" + (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "per_page": 100,
            }
        )
        runs = gh_json(
            ["api", f"repos/{repository}/actions/workflows/merge-ready.yml/runs?{query}"]
        )
        require(runs["total_count"] <= 100, "Readiness refresh history is incomplete")
        title = f"Merge Ready #{candidate['number']} ({head})"
        attempts = [run for run in runs["workflow_runs"] if run["display_title"] == title]
        require(
            all(run["status"] == "completed" for run in attempts),
            "Merge Ready refresh is already running",
        )
        require(len(attempts) < 3, "Merge Ready refresh limit reached for this head in 24 hours")
        # Cooldown expiry and registry recovery do not emit workflow events.
        subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                "merge-ready.yml",
                "--repo",
                repository,
                "--ref",
                "main",
                "-f",
                f"pr={candidate['number']}",
                "-f",
                f"sha={head}",
            ],
            check=True,
            env=dict(os.environ, GH_TOKEN=os.environ["DEPENDABOT_MERGE_TOKEN"]),
        )
    raise NeedsReview("Waiting for successful Merge Ready status")


def require_reports(repository: str, candidate: dict) -> None:
    head = candidate["head"]
    workflows = required_workflows(candidate)
    require(
        all(step["workflow"] in workflows for step in candidate.get("consumer_steps", [])),
        "Consumer coverage references a workflow outside the required set",
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
    policy = json.loads(POLICY.read_text())
    for workflow in sorted(workflows):
        latest, jobs = latest_workflow(repository, head, workflow)
        require(
            latest["head_sha"] == head
            and latest["status"] == "completed"
            and latest["conclusion"] == "success",
            f"Latest {workflow} run did not succeed",
        )
        if not any(
            pull.get("number") == candidate["number"]
            and pull.get("head", {}).get("sha") == head
            and pull.get("base", {}).get("sha") == candidate["base"]
            for pull in latest.get("pull_requests", [])
        ):
            raise StaleBase(
                f"{workflow}: CI did not test the current PR head and base; rebase the PR"
            )
        target_jobs = (
            policy.get("cargo", {})
            .get("targets", {})
            .get(candidate["target"], {})
            .get("workflow_test_jobs", {})
            if candidate["ecosystem"] == "cargo"
            else {}
        )
        required_jobs = target_jobs.get(workflow) or (
            policy["dependency_workflow_test_jobs"]
            .get(candidate["ecosystem"], {})
            .get(workflow, policy["workflow_test_jobs"][workflow])
        )
        # Require coverage markers so incomplete runs cannot satisfy the gate.
        coverage_marker = {
            "ci.yml": "CI coverage scope",
            "e2e.yml": "E2E coverage scope",
            "e2e-ui.yml": "UI coverage scope",
            "web-tests.yml": "Web coverage scope",
        }.get(workflow)
        if coverage_marker and candidate["ecosystem"] in {
            "python",
            "npm",
            "cargo",
            "github-actions",
        }:
            require(
                any(
                    job["name"] == coverage_marker and job["conclusion"] == "success"
                    for job in jobs
                ),
                f"{workflow}: waiting for successful {coverage_marker}",
            )
        for pattern in required_jobs:
            matching = [job for job in jobs if fnmatch.fnmatchcase(job["name"], pattern)]
            # The CI aggregate checks every selected matrix leg; unrelated
            # Rust/parity jobs may intentionally skip on a Python update.
            aggregate_matrix = workflow == "ci.yml" and pattern == "Pytest (*)"
            allowed = {"success", "skipped"} if aggregate_matrix else {"success"}
            require(
                bool(matching)
                and any(job["conclusion"] == "success" for job in matching)
                and all(job["conclusion"] in allowed for job in matching),
                f"{workflow}: waiting for successful {pattern} jobs",
            )
        for consumer in candidate.get("consumer_steps", []):
            if consumer["workflow"] != workflow:
                continue
            require(
                any(
                    job["name"] == consumer["job"]
                    and job["conclusion"] == "success"
                    and any(
                        step["name"] == consumer["step"] and step["conclusion"] == "success"
                        for step in job.get("steps", [])
                    )
                    for job in jobs
                ),
                f"{workflow}: waiting for executed consumer step {consumer['step']}",
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
    target = candidate["target"]
    scope = "dev-tools" if target.startswith("dev/") else "repository"
    report_target = target.removeprefix("dev/") if scope == "dev-tools" else target
    reports = [
        a
        for a in artifacts["artifacts"]
        if a["name"] == f"trivy-cve-report-{scope}" and not a["expired"]
    ]
    require(len(reports) == 1, f"Missing or ambiguous Trivy {scope} report")
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
                report_target,
                candidate["scanner"],
            )
    except (zipfile.BadZipFile, zlib.error) as exc:
        raise NeedsReview(f"Trivy {scope} report is unreadable: {exc}") from exc


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
    for workflow in sorted(required_workflows(candidate)):
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


def require_current_candidate(repository: str, number: int, candidate: dict) -> None:
    current = gh_json(["api", f"repos/{repository}/pulls/{number}"])
    require(
        current["head"]["sha"] == candidate["head"]
        and current["base"]["ref"] == "main"
        and main_head(repository) == candidate["base"],
        "PR or main changed during evaluation",
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
        all(r["state"] != "CHANGES_REQUESTED" for r in latest_decisive_reviews(reviews).values()),
        "A reviewer has requested changes",
    )


def refresh_stale_base(repository: str, candidate: dict, mode: str) -> None:
    if mode != "merge":
        return
    bases = set()
    for workflow in required_workflows(candidate):
        run, _jobs = latest_workflow(repository, candidate["head"], workflow)
        for pull in run.get("pull_requests", []):
            if (
                pull.get("number") == candidate["number"]
                and pull.get("head", {}).get("sha") == candidate["head"]
            ):
                bases.add(pull.get("base", {}).get("sha"))
    require(len(bases) == 1 and None not in bases, "Waiting for CI on one identifiable base")
    previous_base = bases.pop()
    require(previous_base != candidate["base"], "CI metadata needs human inspection")
    # Refresh only an otherwise green candidate, never to hide a failed test.
    require_reports(repository, dict(candidate, base=previous_base))
    approval_refresh_run(repository, candidate)
    commits = paginated(f"repos/{repository}/pulls/{candidate['number']}/commits")
    pull = gh_json(["api", f"repos/{repository}/pulls/{candidate['number']}"])
    require(
        bool(commits)
        and commits[-1]["sha"] == candidate["head"]
        and pull.get("commits") == len(commits),
        "Incomplete commit inventory",
    )
    require(
        sum(len(commit["parents"]) > 1 for commit in commits) < 3,
        "Base refresh limit reached; maintainer review required",
    )
    files = paginated(f"repos/{repository}/pulls/{candidate['number']}/files")
    require(
        len(files) == pull["changed_files"] and bool(files), "Incomplete changed-file inventory"
    )
    paths = {file["filename"] for file in files}
    for sha in (candidate["head"], candidate["base"]):
        require_refresh_permissions(
            refresh_workflows(repository, sha), paths, pull["head"].get("ref")
        )
    require_current_candidate(repository, candidate["number"], candidate)
    try:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                f"repos/{repository}/pulls/{candidate['number']}/update-branch",
                "-f",
                f"expected_head_sha={candidate['head']}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=dict(os.environ, GH_TOKEN=os.environ["DEPENDABOT_MERGE_TOKEN"]),
        )
    except subprocess.CalledProcessError as exc:
        status = re.search(r"HTTP (\d{3})", exc.stderr or "")
        detail = f" (HTTP {status[1]})" if status else ""
        raise NeedsReview(
            f"GitHub rejected branch refresh{detail}; "
            "inspect conflicts, permissions and current head"
        ) from exc
    raise NeedsReview("Updated branch against main; waiting for fresh policy, scans and CI")


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
            refresh_failed_gates(repository, decision, mode)
            try:
                require_reports(repository, decision)
            except StaleBase as stale:
                try:
                    refresh_stale_base(repository, decision, mode)
                except NeedsReview as recovery:
                    raise NeedsReview(f"{stale}; {recovery}") from recovery
                raise
            require_approval(repository, decision, mode)
            require_merge_ready(repository, decision, mode)
            if mode == "merge":
                require_current_candidate(repository, number, decision)
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
