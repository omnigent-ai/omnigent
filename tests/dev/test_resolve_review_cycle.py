"""Resolve must keep iterating until both reviews cover the final PR head."""

import copy
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "dev/resolve-agent/skills/resolve-drive-pr/review_cycle.py"
)
spec = importlib.util.spec_from_file_location("resolve_review_cycle", SCRIPT)
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


class Github:
    def __init__(self):
        self.pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"repo": {"default_branch": "main"}},
        }
        self.comments = [
            self.comment(
                1,
                "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: "
                + "a" * 40
                + " -->\n<!-- polly-review-run:11-1 -->\nNon-blocking: retries can lose data.",
            )
        ]
        self.comments.append(
            self.comment(
                2,
                "<!-- ocr-summary -->\n<!-- ocr-summary-run:10-1 -->\n"
                "🔍 **OpenCodeReview** found **1** issue(s) in this PR.\n"
                "- 📋 Routed to summary by policy: 1 comment(s)\n\n---\n\n"
                "Non-blocking: the retry still loses data.",
                "github-actions[bot]",
            )
        )
        self.reviews = [
            {
                "id": 3,
                "state": "COMMENTED",
                "body": "A race exists.",
                "user": {"login": "maintainer"},
            }
        ]
        self.inline = [
            {
                "id": 4,
                "body": "Optional: closes the wrong connection.",
                "user": {"login": "github-actions[bot]"},
                "commit_id": "a" * 40,
            }
        ]
        self.artifacts = [
            {
                "name": "ocr-completed-7-" + "a" * 40,
                "expired": False,
                "workflow_run": {"id": 10},
            }
        ]
        self.artifacts.append(
            {
                "name": "polly-completed-7-" + "a" * 40,
                "expired": False,
                "workflow_run": {"id": 11},
            }
        )
        self.run = {
            "workflow_id": 20,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
        }
        self.polly_run = {**self.run, "workflow_id": 21}
        self.calls = []
        self.permissions = {}

    @staticmethod
    def comment(id, body, login="omnigent-ci[bot]"):
        return {
            "id": id,
            "body": body,
            "user": {"login": login},
            "html_url": f"https://example.test/comment/{id}",
        }

    def __call__(self, args):
        self.calls.append(args)
        if "POST" in args:
            return None
        path = args[1]
        if path == "repos/o/r/pulls/7":
            return copy.deepcopy(self.pull)
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if "/issues/7/comments?" in path:
            return copy.deepcopy(self.comments)
        if "/pulls/7/reviews?" in path:
            return copy.deepcopy(self.reviews)
        if "/pulls/7/comments?" in path:
            return copy.deepcopy(self.inline)
        if "/collaborators/" in path:
            return {"permission": self.permissions.get(path.split("/")[-2], "write")}
        if "/actions/artifacts?" in path:
            return {"artifacts": copy.deepcopy(self.artifacts)}
        if "/actions/workflows/open-code-review.yml" in path:
            return {"id": 20}
        if "/actions/workflows/polly-review.yml" in path:
            return {"id": 21}
        if "/actions/runs/11" in path:
            return copy.deepcopy(self.polly_run)
        if "/actions/runs/10" in path:
            return copy.deepcopy(self.run)
        raise AssertionError(args)


def handoff(state):
    return {
        "outcome": "fixed",
        "remaining_work": [],
        "review_cycle": {
            "head_sha": state["head_sha"],
            "fingerprint": state["fingerprint"],
            "dispositions": [
                {
                    "key": item["key"],
                    "status": "addressed",
                    "reason": "Fixed in the final commit; regression test passes.",
                }
                for item in state["feedback"]
            ],
        },
    }


def test_collects_nonblocking_approved_commented_and_inline_feedback():
    api = Github()
    api.reviews.append(
        {
            "id": 5,
            "state": "APPROVED",
            "body": "Nit: data races.",
            "user": {"login": "maintainer"},
        }
    )
    state = cycle.snapshot("o/r", 7, api)
    assert state["completed"] == {"polly": True, "ocr": True}
    assert {item["key"] for item in state["feedback"]} == {
        "comment:1",
        "comment:2",
        "review:3",
        "review:5",
        "inline:4",
    }
    cycle.validate(state, handoff(state))


@pytest.mark.parametrize(
    "mutation",
    [
        "expired",
        "failed",
        "unfinished",
        "untrusted_branch",
        "wrong_workflow",
        "wrong_head",
        "no_receipt",
    ],
)
def test_ocr_requires_a_successful_trusted_current_head_receipt(mutation):
    api = Github()
    if mutation == "expired":
        api.artifacts[0]["expired"] = True
    elif mutation == "failed":
        api.run["conclusion"] = "failure"
    elif mutation == "unfinished":
        api.run["status"] = "in_progress"
    elif mutation == "untrusted_branch":
        api.run["head_branch"] = "untrusted"
    elif mutation == "wrong_workflow":
        api.run["workflow_id"] = 99
    elif mutation == "wrong_head":
        api.artifacts[0]["name"] = "ocr-completed-7-" + "b" * 40
    else:
        api.artifacts.clear()
    state = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError, match="ocr"):
        cycle.validate(state, handoff(state))


@pytest.mark.parametrize(
    "login,body",
    [
        (
            "contributor",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->",
        ),
        (
            "unrelated[bot]",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->",
        ),
        ("omnigent-ci[bot]", "<!-- polly-skipped-sha: " + "a" * 40 + " -->"),
        (
            "omnigent-ci[bot]",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "b" * 40 + " -->",
        ),
    ],
)
def test_polly_requires_a_real_current_head_review(login, body):
    api = Github()
    api.comments[0] = api.comment(1, body + "\n<!-- polly-review-run:11-1 -->", login)
    assert not cycle.snapshot("o/r", 7, api)["completed"]["polly"]


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "missing",
        "stale",
        "pending",
        "empty_reason",
        "non_string_reason",
        "duplicate",
        "remaining",
        "missing_remaining",
    ],
)
def test_invalid_handoffs_cannot_claim_ready(mutation):
    state = cycle.snapshot("o/r", 7, Github())
    result = handoff(state)
    if mutation == "partial":
        result["outcome"] = "partially_fixed"
    elif mutation == "missing":
        result["review_cycle"]["dispositions"].pop()
    elif mutation == "stale":
        result["review_cycle"]["head_sha"] = "b" * 40
    elif mutation == "pending":
        result["review_cycle"]["dispositions"][0]["status"] = "non_blocking"
    elif mutation == "empty_reason":
        result["review_cycle"]["dispositions"][0]["reason"] = " "
    elif mutation == "non_string_reason":
        result["review_cycle"]["dispositions"][0]["reason"] = ["unstructured"]
    elif mutation == "duplicate":
        result["review_cycle"]["dispositions"].append(result["review_cycle"]["dispositions"][0])
    elif mutation == "missing_remaining":
        del result["remaining_work"]
    else:
        result["remaining_work"] = ["Fix the retry race."]
    errors = {
        "partial": "outcome must be fixed",
        "missing": "Feedback review:3 needs an evidenced disposition",
        "stale": "receipt is stale or absent",
        "pending": "Feedback comment:1 needs an evidenced disposition",
        "empty_reason": "Feedback comment:1 needs an evidenced disposition",
        "non_string_reason": "Feedback comment:1 needs an evidenced disposition",
        "duplicate": "Duplicate review disposition",
        "remaining": "explicit empty remaining_work list",
        "missing_remaining": "explicit empty remaining_work list",
    }
    with pytest.raises(RuntimeError, match=errors[mutation]):
        cycle.validate(state, result)


def test_do_not_dispatch_completed_reviews():
    api = Github()
    cycle.request_reviews("o/r", 7, cycle.snapshot("o/r", 7, api), api)
    assert not any("POST" in args for args in api.calls)


def test_snapshot_fails_if_head_changes_during_collection():
    api = Github()
    reads = 0

    def request(args):
        nonlocal reads
        if args[1] == "repos/o/r/pulls/7":
            reads += 1
            if reads == 2:
                api.pull["head"]["sha"] = "b" * 40
        return api(args)

    with pytest.raises(RuntimeError, match="changed while collecting"):
        cycle.snapshot("o/r", 7, request)


def test_paginates_reviews_instead_of_dropping_older_findings():
    calls = []

    def request(args):
        calls.append(args)
        return [{"id": i} for i in range(100)] if args[1].endswith("page=1") else [{"id": 100}]

    assert len(cycle.pages("reviews", request)) == 101
    assert len(calls) == 2


def test_ocr_coverage_is_observed_before_collecting_its_findings():
    api = Github()
    cycle.snapshot("o/r", 7, api)
    paths = [args[1] for args in api.calls]
    receipt = paths.index("repos/o/r/actions/runs/10")
    for suffix in ("issues/7/comments", "pulls/7/comments", "pulls/7/reviews"):
        assert receipt < next(i for i, path in enumerate(paths) if suffix in path)


@pytest.mark.parametrize("change", ["edit", "new_comment", "push", "other_pr"])
def test_live_feedback_changes_invalidate_a_handoff(change):
    api = Github()
    first = cycle.snapshot("o/r", 7, api)
    if change == "edit":
        api.inline[0]["body"] += " Also leaks."
    elif change == "new_comment":
        api.comments.append(api.comment(8, "<!-- ocr-summary -->\nMissed failure."))
    elif change == "push":
        api.pull["head"]["sha"] = "b" * 40
        api.comments[0]["body"] = api.comments[0]["body"].replace("a" * 40, "b" * 40)
        for artifact in api.artifacts:
            artifact["name"] = artifact["name"].replace("a" * 40, "b" * 40)
    refreshed = cycle.snapshot("o/r", 7, api)
    if change == "other_pr":
        refreshed = cycle.snapshot(
            "o/other",
            7,
            lambda args: api([arg.replace("repos/o/other/", "repos/o/r/") for arg in args]),
        )
    assert all(refreshed["completed"].values())
    with pytest.raises(RuntimeError, match="receipt is stale or absent"):
        cycle.validate(refreshed, handoff(first))


def test_each_push_requires_fresh_evidence_from_both_reviewers():
    api = Github()
    for round_number in range(1, 3):
        head = f"{round_number:040x}"
        api.pull["head"]["sha"] = head
        pending = cycle.snapshot("o/r", 7, api)
        assert pending["completed"] == {"polly": False, "ocr": False}
        with pytest.raises(RuntimeError, match="missing or incomplete"):
            cycle.validate(pending, handoff(pending))
        api.calls.clear()
        cycle.request_reviews("o/r", 7, pending, api)
        dispatches = [args for args in api.calls if "POST" in args]
        assert len(dispatches) == 2
        assert all("ref=main" in args and "inputs[pr]=7" in args for args in dispatches)
        assert {args[3].split("/")[-2] for args in dispatches} == set(cycle.REVIEWERS.values())
        api.comments.append(
            api.comment(
                100 + round_number,
                f"<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: {head} -->\n"
                "<!-- polly-review-run:11-1 -->\nNo findings.",
            )
        )
        api.artifacts[0]["name"] = f"ocr-completed-7-{head}"
        api.artifacts[1]["name"] = f"polly-completed-7-{head}"
        complete = cycle.snapshot("o/r", 7, api)
        cycle.validate(complete, handoff(complete))


def test_failed_review_collection_cannot_produce_a_receipt():
    api = Github()

    def request(args):
        if "/pulls/7/comments?" in args[1]:
            raise RuntimeError("GitHub unavailable")
        return api(args)

    with pytest.raises(RuntimeError, match="unavailable"):
        cycle.snapshot("o/r", 7, request)


def test_human_notes_and_dismissed_reviews_still_need_dispositions():
    api = Github()
    api.comments.append(api.comment(8, "Non-blocking: the retry still loses data.", "maintainer"))
    api.comments.append(api.comment(9, "/review", "maintainer"))
    api.comments.append(api.comment(10, "Deploy succeeded.", "github-actions[bot]"))
    api.comments.append(api.comment(11, "Untrusted request.", "outsider"))
    api.permissions["outsider"] = "read"
    api.reviews[0]["state"] = "DISMISSED"
    state = cycle.snapshot("o/r", 7, api)
    keys = {item["key"] for item in state["feedback"]}
    assert "comment:8" in keys
    assert "review:3" in keys
    assert not keys & {"comment:9", "comment:10", "comment:11"}
    result = handoff(state)
    result["review_cycle"]["dispositions"] = [
        item for item in result["review_cycle"]["dispositions"] if item["key"] != "comment:8"
    ]
    with pytest.raises(RuntimeError, match="comment:8"):
        cycle.validate(state, result)


@pytest.mark.parametrize(
    "mutation", ["deleted", "wrong_run", "wrong_author", "wrong_attempt", "quoted"]
)
def test_ocr_completion_requires_its_published_summary(mutation):
    api = Github()
    if mutation == "deleted":
        api.comments.pop(1)
    elif mutation == "wrong_run":
        api.comments[1]["body"] = api.comments[1]["body"].replace("10-1", "11-1")
    elif mutation == "wrong_author":
        api.comments[1]["user"]["login"] = "maintainer"
    elif mutation == "quoted":
        api.comments[1]["body"] = "Quoted review:\n" + api.comments[1]["body"]
    else:
        api.run["run_attempt"] = 2
    state = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError, match="ocr"):
        cycle.validate(state, handoff(state))


@pytest.fixture
def github_cli(monkeypatch):
    api = Github()

    def run(args, **kwargs):
        assert args[0] == "gh"
        result = api(args[1:])
        return subprocess.CompletedProcess(
            args, 0, "" if result is None else json.dumps(result), ""
        )

    monkeypatch.setattr(cycle.subprocess, "run", run)
    return api


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_permission_lookup_only_ignores_not_found(monkeypatch, status):
    api = Github()
    api.comments.append(api.comment(12, "Please change this.", "outsider"))

    def run(args, **kwargs):
        if args[2] == "repos/o/r/collaborators/outsider/permission":
            raise subprocess.CalledProcessError(
                1, args, stderr=f"gh: Permission lookup failed (HTTP {status})"
            )
        return subprocess.CompletedProcess(args, 0, json.dumps(api(args[1:])), "")

    monkeypatch.setattr(cycle.subprocess, "run", run)
    if status == 404:
        state = cycle.snapshot("o/r", 7)
        assert "comment:12" not in {item["key"] for item in state["feedback"]}
        cycle.validate(state, handoff(state))
    else:
        with pytest.raises(cycle.GitHubError, match=f"Permission lookup failed.*{status}"):
            cycle.snapshot("o/r", 7)


def test_unknown_dispositions_cannot_be_carried_into_a_new_snapshot():
    state = cycle.snapshot("o/r", 7, Github())
    result = handoff(state)
    result["review_cycle"]["dispositions"].append(
        {"key": "comment:deleted", "status": "addressed", "reason": "Old finding."}
    )
    with pytest.raises(RuntimeError, match="unknown feedback: comment:deleted"):
        cycle.validate(state, result)


@pytest.mark.parametrize("missing", ["ocr_summary", "polly_review", "both"])
def test_cli_requests_force_review_when_evidence_is_missing(
    github_cli, monkeypatch, capsys, missing
):
    if missing == "ocr_summary":
        github_cli.comments.pop(1)
        expected = {"open-code-review.yml"}
    elif missing == "polly_review":
        github_cli.comments[0]["body"] = "<!-- polly-skipped-sha: " + "a" * 40 + " -->"
        expected = {"polly-review.yml"}
    else:
        github_cli.pull["head"]["sha"] = "b" * 40
        expected = set(cycle.REVIEWERS.values())
    monkeypatch.setattr(
        sys, "argv", ["review_cycle.py", "request", "--repository", "o/r", "--pr-number", "7"]
    )
    assert cycle.main() == 0
    assert json.loads(capsys.readouterr().out)["head_sha"] == github_cli.pull["head"]["sha"]
    dispatches = [args for args in github_cli.calls if "POST" in args]
    assert {args[3].split("/")[-2] for args in dispatches} == expected
    assert all(
        args[-6:] == ["-f", "ref=main", "-f", "inputs[pr]=7", "-f", "inputs[force]=true"]
        for args in dispatches
    )


def test_cli_check_returns_failure_for_stale_handoff(github_cli, monkeypatch, tmp_path, capsys):
    first = cycle.snapshot("o/r", 7)
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(handoff(first)))
    github_cli.inline[0]["body"] += " Updated finding."
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_cycle.py",
            "check",
            "--repository",
            "o/r",
            "--pr-number",
            "7",
            "--handoff",
            str(path),
        ],
    )
    assert cycle.main() == 1
    assert "receipt is stale or absent" in capsys.readouterr().out


def test_cli_preserves_github_error_details(monkeypatch, capsys):
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(
            1, args, stderr="gh: API rate limit exceeded (HTTP 403)"
        )

    monkeypatch.setattr(cycle.subprocess, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["review_cycle.py", "snapshot", "--repository", "o/r", "--pr-number", "7"]
    )
    assert cycle.main() == 1
    assert "API rate limit exceeded (HTTP 403)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "event,branch",
    [
        ("pull_request_target", "contributor-branch"),
        ("issue_comment", "main"),
    ],
)
def test_ocr_accepts_trusted_target_and_comment_events(event, branch):
    api = Github()
    api.run.update(event=event, head_branch=branch)
    state = cycle.snapshot("o/r", 7, api)
    cycle.validate(state, handoff(state))


@pytest.mark.parametrize(
    "mutation",
    [
        "no_receipt",
        "wrong_workflow",
        "untrusted_branch",
        "pull_request",
        "quoted",
        "wrong_run",
    ],
)
def test_polly_cannot_be_completed_by_injected_bot_markers(mutation):
    api = Github()
    if mutation == "no_receipt":
        api.artifacts.pop(1)
    elif mutation == "wrong_workflow":
        api.polly_run["workflow_id"] = 20
    elif mutation == "untrusted_branch":
        api.polly_run["head_branch"] = "contributor-branch"
    elif mutation == "pull_request":
        api.polly_run["event"] = "pull_request"
    elif mutation == "quoted":
        api.comments[0]["body"] = (
            "<!-- ocr-summary -->\nQuoted review:\n" + api.comments[0]["body"]
        )
    else:
        api.comments[0]["body"] = api.comments[0]["body"].replace("11-1", "12-1")
    state = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError, match="missing or incomplete: polly"):
        cycle.validate(state, handoff(state))


@pytest.mark.parametrize("stdout", ["", "null", "[]"])
def test_cli_rejects_invalid_pull_response_with_context(monkeypatch, capsys, stdout):
    monkeypatch.setattr(
        cycle.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout, ""),
    )
    monkeypatch.setattr(
        sys, "argv", ["review_cycle.py", "snapshot", "--repository", "o/r", "--pr-number", "7"]
    )
    assert cycle.main() == 1
    assert "Expected a JSON object from repos/o/r/pulls/7" in capsys.readouterr().out


def test_ocr_accepts_captured_nonsticky_summary():
    api = Github()
    # Public bot payload; fixture retains only fields consumed by the helper.
    captured = json.loads((Path(__file__).parent / "fixtures/ocr_summary.json").read_text())
    api.comments[1] = captured
    api.artifacts[0]["workflow_run"]["id"] = 36565789828

    def request(args):
        if args[1] == "repos/o/r/actions/runs/36565789828":
            return copy.deepcopy(api.run)
        return api(args)

    state = cycle.snapshot("o/r", 7, request)
    assert state["completed"]["ocr"] is True
    assert (
        next(item for item in state["feedback"] if item["key"] == "comment:5890039813")["body"]
        == captured["body"].strip()
    )


def test_cli_check_requires_handoff_before_network_access(github_cli, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["review_cycle.py", "check", "--repository", "o/r", "--pr-number", "7"]
    )
    with pytest.raises(SystemExit) as exc:
        cycle.main()
    assert exc.value.code == 2
    assert "check requires --handoff" in capsys.readouterr().err
    assert github_cli.calls == []


@pytest.mark.parametrize("response", [None, [], {}, {"artifacts": None}, {"artifacts": {}}])
def test_invalid_paginated_artifact_response_fails_cleanly(monkeypatch, capsys, response):
    api = Github()

    def run(args, **kwargs):
        result = response if "/actions/artifacts?" in args[2] else api(args[1:])
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    monkeypatch.setattr(cycle.subprocess, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["review_cycle.py", "snapshot", "--repository", "o/r", "--pr-number", "7"]
    )
    assert cycle.main() == 1
    assert "Invalid paginated response for repos/o/r/actions/artifacts?" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "wrong_pr",
        "wrong_head",
        "other_comment",
        "other_repository",
        "invalid_url",
        "missing_comment",
        "human",
        "nonzero",
        "extra_finding",
        "no_receipt",
        "failed_run",
        "untrusted_run",
        "expired",
        "stale_artifact",
    ],
)
def test_zero_findings_requires_matching_trusted_receipt(monkeypatch, mutation):
    api = Github()
    api.artifacts[0]["id"] = 123
    api.comments[1]["body"] = (
        "<!-- ocr-summary -->\n✅ **OpenCodeReview**: "
        "Review complete: 0 finding(s) across 4 selected item(s)."
    )
    receipt = {
        "pr": 7,
        "head": "a" * 40,
        "summary_url": "https://github.com/o/r/pull/7#issuecomment-2",
    }
    if mutation == "wrong_pr":
        receipt["pr"] = 8
    elif mutation == "wrong_head":
        receipt["head"] = "b" * 40
    elif mutation == "other_comment":
        receipt["summary_url"] = "https://github.com/o/r/pull/7#issuecomment-99"
    elif mutation == "other_repository":
        receipt["summary_url"] = "https://github.com/other/repo/pull/7#issuecomment-2"
    elif mutation == "invalid_url":
        receipt["summary_url"] = []
    elif mutation == "missing_comment":
        api.comments.pop(1)
    elif mutation == "human":
        api.comments[1]["user"]["login"] = "maintainer"
    elif mutation == "nonzero":
        api.comments[1]["body"] = api.comments[1]["body"].replace("0 finding", "1 finding")
    elif mutation == "extra_finding":
        api.comments[1]["body"] += "\nBlocking: still loses data."
    elif mutation == "no_receipt":
        api.artifacts.clear()
    elif mutation == "failed_run":
        api.run["conclusion"] = "failure"
    elif mutation == "untrusted_run":
        api.run["head_branch"] = "untrusted"
    elif mutation == "expired":
        api.artifacts[0]["expired"] = True
    elif mutation == "stale_artifact":
        api.artifacts[0]["name"] = "ocr-completed-7-" + "b" * 40
    downloads = []

    def read(repository, artifact_id):
        downloads.append((repository, artifact_id))
        return receipt

    monkeypatch.setattr(cycle, "ocr_receipt", read)
    state = cycle.snapshot("o/r", 7, api)
    assert state["completed"]["ocr"] == (mutation == "valid")
    if mutation == "valid":
        assert downloads == [("o/r", 123)]
        result = handoff(state)
        result["review_cycle"]["dispositions"][1].update(
            status="not_needed", reason="OCR reported zero findings on this commit."
        )
        cycle.validate(state, result)
        # A clean OCR review does not excuse unrelated human or inline findings.
        result["review_cycle"]["dispositions"].pop()
        with pytest.raises(RuntimeError, match="needs an evidenced disposition"):
            cycle.validate(state, result)
    elif mutation not in {
        "wrong_pr",
        "wrong_head",
        "other_comment",
        "other_repository",
        "invalid_url",
    }:
        assert downloads == []


@pytest.mark.parametrize(
    "payload", ["valid", "bad_zip", "missing_file", "bad_json", "list", "oversized", "http_error"]
)
def test_download_ocr_completion_receipt(monkeypatch, payload):
    expected = {"pr": 7, "head": "a" * 40, "summary_url": "summary-url"}
    archive = io.BytesIO()
    with ZipFile(archive, "w") as bundle:
        content = {"bad_json": "{", "list": "[]", "oversized": " " * 65537}.get(
            payload, json.dumps(expected)
        )
        bundle.writestr(
            "other.json" if payload == "missing_file" else "ocr-completion.json", content
        )

    def run(args, **kwargs):
        assert args == ["gh", "api", "repos/o/r/actions/artifacts/123/zip"]
        assert kwargs == {"check": True, "capture_output": True, "timeout": 120}
        if payload == "http_error":
            raise subprocess.CalledProcessError(1, args, stderr=b"HTTP 403")
        return subprocess.CompletedProcess(
            args, 0, b"invalid" if payload == "bad_zip" else archive.getvalue()
        )

    monkeypatch.setattr(cycle.subprocess, "run", run)
    if payload == "valid":
        assert cycle.ocr_receipt("o/r", 123) == expected
    else:
        with pytest.raises((RuntimeError, ValueError, subprocess.CalledProcessError)):
            cycle.ocr_receipt("o/r", 123)
