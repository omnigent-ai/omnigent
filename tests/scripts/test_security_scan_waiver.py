"""Exercise security-scan waivers with real shell and jq evaluation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / ".github/scripts/security-scan/should-scan.sh"


def _review(login: str, state: str, review_id: int = 1) -> dict[str, object]:
    return {
        "user": {"login": login},
        "state": state,
        "id": review_id,
        "submitted_at": "2026-09-24T00:00:00Z",
    }


def _run_scan(
    tmp_path: Path,
    reviews: list[list[dict[str, object]]],
    *,
    label: bool = True,
    maintainers: str = "alice bob",
    author: str = "contributor",
    failure: str = "",
    token: str = "test-token",
    event: str = "pull_request",
    association: str = "CONTRIBUTOR",
) -> tuple[bool, str]:
    (tmp_path / "pull.json").write_text(
        json.dumps(
            {
                "author": author,
                "labels": [{"name": "skip-security-scan"}] if label else [],
            }
        )
    )
    (tmp_path / "reviews.json").write_text(json.dumps(reviews))
    gh = tmp_path / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "pr view" ]]; then
  jq -r .author "$WAIVER_FIXTURES/pull.json"
  exit 0
fi
[[ "$1" == api ]]
endpoint=$2
shift 2
query='.'
has_query=false
paginate=false
slurp=false
while (( $# )); do
  case "$1" in
    --jq) query=$2; has_query=true; shift ;;
    --paginate) paginate=true ;;
    --slurp) slurp=true ;;
    *) exit 2 ;;
  esac
  shift
done
[[ "$slurp" != true || "$has_query" != true ]] || exit 2
case "$endpoint" in
  'repos/example/project/pulls/42')
    [[ "$WAIVER_FAILURE" != labels ]] || exit 1
    jq -r "$query" "$WAIVER_FIXTURES/pull.json"
    ;;
  'repos/example/project/pulls/42/reviews?per_page=100')
    [[ "$WAIVER_FAILURE" != reviews ]] || exit 1
    [[ "$paginate" == true && "$slurp" == true ]]
    jq -r "$query" "$WAIVER_FIXTURES/reviews.json"
    [[ "$WAIVER_FAILURE" != partial-reviews ]] || exit 1
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        [shutil.which("bash") or "bash", _SCRIPT.as_posix()],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "WAIVER_FIXTURES": tmp_path.as_posix(),
            "WAIVER_FAILURE": failure,
            "GITHUB_OUTPUT": output.as_posix(),
            "GH_TOKEN": token,
            "REPO": "example/project",
            "PR": "42",
            "PR_AUTHOR": author,
            "MAINTAINERS": maintainers,
            "EVENT_NAME": event,
            "AUTHOR_ASSOCIATION": association,
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values["scan"] == "true", values["reason"]


@pytest.mark.parametrize(
    "label,reviews,scan",
    [
        (True, [[]], True),
        (False, [[_review("alice", "APPROVED")]], True),
        (True, [[_review("outsider", "APPROVED")]], True),
        (True, [[_review("alice", "COMMENTED")]], True),
        (True, [[_review("alice", "PENDING")]], True),
        (True, [[_review("alice", "APPROVED")]], False),
    ],
)
def test_waiver_requires_label_and_maintainer_approval(tmp_path, label, reviews, scan):
    assert _run_scan(tmp_path, reviews, label=label)[0] is scan


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_later_decisive_review_revokes_approval_across_pages(tmp_path, state):
    reviews = [[_review("Alice", "APPROVED", 9)], [_review("ALICE", state, 10)]]
    assert _run_scan(tmp_path, reviews)[0]


def test_comment_does_not_revoke_approval(tmp_path):
    reviews = [[_review("alice", "APPROVED")], [_review("alice", "COMMENTED", 2)]]
    assert not _run_scan(tmp_path, reviews)[0]


def test_new_approval_restores_waiver_regardless_of_response_order(tmp_path):
    reviews = [[_review("Alice", "APPROVED", 10)], [_review("alice", "CHANGES_REQUESTED", 9)]]
    assert not _run_scan(tmp_path, reviews, maintainers="ALICE BOB")[0]


def test_submission_time_takes_precedence_over_id(tmp_path):
    withdrawn = _review("alice", "CHANGES_REQUESTED", 1)
    withdrawn["submitted_at"] = "2026-09-24T01:00:00Z"
    assert _run_scan(tmp_path, [[withdrawn, _review("alice", "APPROVED", 10)]])[0]


def test_other_maintainer_can_still_approve(tmp_path):
    reviews = [[_review("alice", "DISMISSED"), _review("bob", "APPROVED")]]
    assert not _run_scan(tmp_path, reviews)[0]


@pytest.mark.parametrize("failure", ["labels", "reviews", "partial-reviews"])
def test_api_failure_cannot_waive_scanning(tmp_path, failure):
    assert _run_scan(tmp_path, [[_review("alice", "APPROVED")]], failure=failure)[0]


@pytest.mark.parametrize("maintainers", ["", "   ", "somebody-else"])
def test_missing_maintainer_identity_cannot_waive_scanning(tmp_path, maintainers):
    assert _run_scan(tmp_path, [[_review("alice", "APPROVED")]], maintainers=maintainers)[0]


def test_missing_credentials_cannot_waive_scanning(tmp_path):
    assert _run_scan(tmp_path, [[_review("alice", "APPROVED")]], token="")[0]


def test_maintainer_author_remains_trusted(tmp_path):
    assert not _run_scan(tmp_path, [[]], author="ALICE", label=False)[0]


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_associations_remain_trusted(tmp_path, association):
    assert not _run_scan(tmp_path, [[]], association=association, label=False)[0]


def test_non_pr_event_remains_trusted(tmp_path):
    assert not _run_scan(tmp_path, [[]], event="push", label=False)[0]


def test_review_events_apply_current_waiver_state(tmp_path):
    assert _run_scan(tmp_path, [[]], event="pull_request_review")[0]


@pytest.mark.parametrize("name", ["security-scan.yml", "rerun-security-gate.yml"])
def test_review_submission_and_dismissal_trigger_reevaluation(name):
    workflow = yaml.load(
        (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert {"submitted", "dismissed"} <= set(workflow["on"]["pull_request_review"]["types"])
