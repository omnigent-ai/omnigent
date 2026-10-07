#!/usr/bin/env python3
"""Collect independent reviews and enforce a current-head Resolve review receipt.

A successful review run is evidence of coverage, not of correctness. Resolve
must still give each feedback document a concrete disposition (including every
finding within a summary). GitHub review states/severity labels never waive it.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode
from zipfile import BadZipFile, ZipFile

REVIEWERS = {"polly": "polly-review.yml", "ocr": "open-code-review.yml"}
REVIEW_BOTS = {"github-actions[bot]", "omnigent-ci[bot]"}
RESOLVE_BOT = "omni-resolve-agent[bot]"
PERMISSIONS = {"admin", "maintain", "write", "push"}


class GitHubError(RuntimeError):
    def __init__(self, error: subprocess.CalledProcessError):
        detail = (error.stderr or "").strip() or str(error)
        status = re.search(r"\(HTTP (\d{3})\)", detail)
        self.status = int(status[1]) if status else None
        super().__init__(detail)


def gh_json(args: list[str]) -> object:
    try:
        result = subprocess.run(
            ["gh", *args], check=True, text=True, capture_output=True, timeout=120
        )
    except subprocess.CalledProcessError as exc:
        raise GitHubError(exc) from exc
    return json.loads(result.stdout) if result.stdout.strip() else None


def api_object(endpoint, request):
    result = request(["api", endpoint])
    if not isinstance(result, dict):
        raise RuntimeError(f"Expected a JSON object from {endpoint}")
    return result


def pages(endpoint, request, field=None):
    result = []
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 10001):
        batch = request(["api", f"{endpoint}{separator}per_page=100&page={page}"])
        if field:
            if not isinstance(batch, dict) or field not in batch:
                raise RuntimeError(f"Invalid paginated response for {endpoint}: missing {field}")
            batch = batch[field]
        if not isinstance(batch, list):
            raise RuntimeError(f"Invalid paginated response for {endpoint}")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError(f"Pagination did not finish for {endpoint}")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def actor(item):
    return str((item.get("user") or {}).get("login") or "").casefold()


def completed_review_runs(repository, number, head, default_branch, reviewer, request):
    name = f"{reviewer}-completed-{number}-{head}"
    artifacts = pages(
        f"repos/{repository}/actions/artifacts?{urlencode({'name': name})}",
        request,
        "artifacts",
    )
    if not artifacts:
        return {}
    workflow = api_object(f"repos/{repository}/actions/workflows/{REVIEWERS[reviewer]}", request)
    markers = {}
    for artifact in artifacts:
        if artifact.get("name") != name or artifact.get("expired"):
            continue
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if not run_id:
            continue
        run = api_object(f"repos/{repository}/actions/runs/{run_id}", request)
        trusted = (reviewer == "ocr" and run.get("event") == "pull_request_target") or (
            run.get("head_branch") == default_branch
            and run.get("event") in {"issue_comment", "workflow_dispatch"}
        )
        if (
            trusted
            and run.get("workflow_id") == workflow["id"]
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            markers[f"{run_id}-{run['run_attempt']}"] = artifact.get("id")
    return markers


def ocr_receipt(repository, artifact_id):
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    try:
        with ZipFile(io.BytesIO(result.stdout)) as archive:
            info = archive.getinfo("ocr-completion.json")
            if info.file_size > 65536:
                raise RuntimeError("OCR completion receipt is too large")
            receipt = json.loads(archive.read(info))
    except (BadZipFile, KeyError) as exc:
        raise RuntimeError("Invalid OCR completion artifact") from exc
    if not isinstance(receipt, dict):
        raise RuntimeError("OCR completion receipt must be an object")
    return receipt


def completed_zero_findings(repository, number, head, comments, artifacts):
    # OCR's zero-findings path omits the run marker. Bind the exact bot comment
    # through the trusted workflow's receipt instead; text alone is insufficient.
    summary_urls = {
        f"https://github.com/{repository}/pull/{number}#issuecomment-{item['id']}"
        for item in comments
        if actor(item) == "github-actions[bot]"
        and re.fullmatch(
            r"<!-- ocr-summary -->\n✅ \*\*OpenCodeReview\*\*: Review complete: "
            r"0 finding\(s\) across \d+ selected item\(s\)\.",
            str(item.get("body") or "").strip(),
        )
    }
    if not summary_urls:
        return False
    for artifact_id in artifacts.values():
        if not artifact_id:
            continue
        receipt = ocr_receipt(repository, artifact_id)
        if (
            receipt.get("pr") == number
            and receipt.get("head") == head
            and isinstance(receipt.get("summary_url"), str)
            and receipt["summary_url"] in summary_urls
        ):
            return True
    return False


def snapshot(repository: str, number: int, request=gh_json):
    pull = api_object(f"repos/{repository}/pulls/{number}", request)
    if pull.get("state") != "open" or pull.get("draft"):
        raise RuntimeError("PR is closed or draft; review cycle cannot complete")
    head = pull["head"]["sha"]
    # Observe completed publication before collecting findings from either reviewer.
    completed_runs = {
        reviewer: completed_review_runs(
            repository, number, head, pull["base"]["repo"]["default_branch"], reviewer, request
        )
        for reviewer in REVIEWERS
    }
    comments = pages(f"repos/{repository}/issues/{number}/comments", request)
    reviews = pages(f"repos/{repository}/pulls/{number}/reviews", request)
    inline = pages(f"repos/{repository}/pulls/{number}/comments", request)
    permissions = {}

    def trusted(item):
        login = actor(item)
        if login in REVIEW_BOTS:
            return True
        if not login or login == RESOLVE_BOT or login.endswith("[bot]"):
            return False
        if login not in permissions:
            try:
                permission = api_object(
                    f"repos/{repository}/collaborators/{login}/permission", request
                )
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                permission = {}
            permissions[login] = permission.get("permission") in PERMISSIONS
        return permissions[login]

    feedback = []
    for kind, items in (("comment", comments), ("review", reviews), ("inline", inline)):
        for item in items:
            body = str(item.get("body") or "").strip()
            if not body:
                continue
            if kind == "comment":
                if body in {"/review", "/review force", "/ocr", "/ocr force"}:
                    continue
                # Keep review summaries, but exclude unrelated bot chatter.
                if actor(item) in REVIEW_BOTS and not (
                    "<!-- polly-review-bot -->" in body.splitlines()
                    or "<!-- ocr-summary -->" in body.splitlines()
                ):
                    continue
            if not trusted(item):
                continue
            if kind == "review" and item.get("state") == "PENDING":
                continue
            feedback.append(
                {
                    "key": f"{kind}:{item['id']}",
                    "body": body,
                    "url": item.get("html_url", ""),
                    "author": actor(item),
                    "path": item.get("path"),
                    "line": item.get("line"),
                    "commit_id": item.get("commit_id"),
                    "updated_at": item.get("updated_at") or item.get("submitted_at") or "",
                }
            )
    feedback.sort(key=lambda item: item["key"])
    completed = {
        "polly": any(
            actor(item) in REVIEW_BOTS
            and str(item.get("body") or "").splitlines()[:2]
            == ["<!-- polly-review-bot -->", f"<!-- polly-reviewed-sha: {head} -->"]
            and any(
                f"<!-- polly-review-run:{run} -->" in str(item.get("body") or "").splitlines()[:3]
                for run in completed_runs["polly"]
            )
            for item in comments
        ),
        "ocr": any(
            actor(item) == "github-actions[bot]"
            and any(
                str(item.get("body") or "").splitlines()[:2]
                == ["<!-- ocr-summary -->", f"<!-- ocr-summary-run:{run} -->"]
                for run in completed_runs["ocr"]
            )
            for item in comments
        ),
    }
    if not completed["ocr"]:
        completed["ocr"] = completed_zero_findings(
            repository, number, head, comments, completed_runs["ocr"]
        )
    # Re-read after pagination: never bind a mixed-head snapshot to a receipt.
    current = api_object(f"repos/{repository}/pulls/{number}", request)
    if current.get("state") != "open" or current.get("draft") or current["head"]["sha"] != head:
        raise RuntimeError("PR changed while collecting reviews; refresh the snapshot")
    result = {
        "repository": repository,
        "pr_number": number,
        "head_sha": head,
        "completed": completed,
        "feedback": feedback,
    }
    result["fingerprint"] = digest(result)
    return result


def validate(state, handoff):
    if not isinstance(handoff, dict):
        raise RuntimeError("Handoff must be an object")
    if handoff.get("outcome") != "fixed":
        raise RuntimeError(
            "Review cycle is incomplete: outcome must be fixed, not partially_fixed"
        )
    missing = [name for name, complete in state["completed"].items() if not complete]
    if missing:
        raise RuntimeError("Current-head reviews missing or incomplete: " + ", ".join(missing))
    receipt = handoff.get("review_cycle") or {}
    if not isinstance(receipt, dict):
        raise RuntimeError("Review-cycle receipt must be an object")
    if (
        receipt.get("head_sha") != state["head_sha"]
        or receipt.get("fingerprint") != state["fingerprint"]
    ):
        raise RuntimeError("Review-cycle receipt is stale or absent; evaluate a fresh snapshot")
    decisions = receipt.get("dispositions")
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise RuntimeError("Review dispositions must be a list of records")
    if any(not isinstance(item.get("key"), str) for item in decisions):
        raise RuntimeError("Review dispositions require feedback keys")
    by_key = {item["key"]: item for item in decisions}
    if len(by_key) != len(decisions):
        raise RuntimeError("Duplicate review disposition")
    unknown = set(by_key) - {item["key"] for item in state["feedback"]}
    if unknown:
        raise RuntimeError(
            "Dispositions reference unknown feedback: " + ", ".join(sorted(unknown))
        )
    for item in state["feedback"]:
        decision = by_key.get(item["key"], {})
        if (
            decision.get("status") not in {"addressed", "invalid", "not_needed"}
            or not isinstance(decision.get("reason"), str)
            or not decision["reason"].strip()
        ):
            raise RuntimeError(
                f"Feedback {item['key']} needs an evidenced disposition; "
                "non-blocking is not a waiver"
            )
    if handoff.get("remaining_work") != []:
        raise RuntimeError("Review cycle requires an explicit empty remaining_work list")


def request_reviews(repository, number, state, request=gh_json):
    """Explicit dispatch is the supported bot equivalent of /review and /ocr."""
    repo = api_object(f"repos/{repository}", request)
    for name, workflow in REVIEWERS.items():
        if not state["completed"][name]:
            request(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repository}/actions/workflows/{workflow}/dispatches",
                    "-f",
                    f"ref={repo['default_branch']}",
                    "-f",
                    f"inputs[pr]={number}",
                    "-f",
                    "inputs[force]=true",
                ]
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "request", "check"])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--handoff", type=Path)
    args = parser.parse_args()
    if args.command == "check" and not args.handoff:
        parser.error("check requires --handoff")
    try:
        state = snapshot(args.repository, args.pr_number)
        if args.command == "request":
            request_reviews(args.repository, args.pr_number, state)
        if args.command == "check":
            validate(state, json.loads(args.handoff.read_text()))
        print(json.dumps(state, indent=2))
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Review cycle not complete ({type(exc).__name__}): {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
