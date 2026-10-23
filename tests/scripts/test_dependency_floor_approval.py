"""Tests for ``.github/scripts/dependency_floor_approval.py``.

A young-dependency-floor waiver is self-attested, so the Maintainer Approval job
also requires a maintainer other than the PR author to add the approval label
and approve a commit carrying every waiver the PR newly introduces.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

import dev.lint.lint_young_dependency_floor as lint

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
REPO = "example/project"
LABEL = {"dependency-floor-approved"}
WAIVER = "    # custom-lint: disable-next=young-dependency-floor -- security fix, mirror checked"
CERTIFI = '"certifi>=2026.10.19",'
PILLOW = '"pillow>=13,<14",'


def _pyproject(*requirement_lines: str) -> str:
    return "\n".join(["[project]", "dependencies = [", *requirement_lines, "]", ""])


@pytest.fixture
def approval(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "dependency_floor_approval", SCRIPTS / "dependency_floor_approval.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclass resolves annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_waived_requirements_follow_the_lint_directive_forms(approval) -> None:
    same_line = (
        '    "a>=1",  # custom-lint: disable=workspace-scoped-cache, young-dependency-floor'
    )
    other_rule = '    "b>=1",  # custom-lint: disable=workspace-scoped-cache'
    source = _pyproject(WAIVER, f"    {CERTIFI}", same_line, other_rule)
    assert approval.waived_requirements(source) == {CERTIFI, same_line.strip()}


def _labeled(login: str, label: str = "dependency-floor-approved") -> dict:
    return {"event": "labeled", "actor": {"login": login}, "label": {"name": label}}


def _review(login: str, state: str = "APPROVED", sha: str = "head", at: str = "2") -> dict:
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at}


@pytest.mark.parametrize(
    ("labels", "timeline", "reviews", "approved", "reason"),
    [
        (set(), [], [], False, "add the `dependency-floor-approved` label, and approve"),
        (LABEL, [_labeled("author")], [], False, "added by the author"),
        (LABEL, [_labeled("outsider")], [], False, "non-maintainer @outsider"),
        (
            LABEL,
            [_labeled("reviewer"), _labeled("author"), _labeled("reviewer", "automerge")],
            [_review("reviewer")],
            False,
            "added by the author",
        ),
        (LABEL, [_labeled("Reviewer")], [], False, "@Reviewer added"),
        (
            LABEL,
            [_labeled("reviewer")],
            [_review("reviewer"), _review("reviewer", "CHANGES_REQUESTED", at="3")],
            False,
            "has not approved",
        ),
        (
            LABEL,
            [_labeled("reviewer")],
            [_review("reviewer"), _review("reviewer", "DISMISSED", at="3")],
            False,
            "has not approved",
        ),
        (LABEL, [_labeled("reviewer")], [_review("author")], False, "has not approved"),
        (LABEL, [], [_review("reviewer")], False, "No `dependency-floor-approved` labeled event"),
        (LABEL, [_labeled("reviewer")], [_review("reviewer", sha="")], False, "names no commit"),
        (LABEL, [_labeled("Reviewer")], [_review("reviewer")], True, "@Reviewer at head"),
        (
            LABEL,
            [_labeled("reviewer")],
            [_review("reviewer"), _review("reviewer", "COMMENTED", sha="other", at="3")],
            True,
            "@reviewer at head",
        ),
        (LABEL, [_labeled("reviewer")], [_review("reviewer", sha="rebased")], True, "at rebased"),
        (
            LABEL,
            [_labeled("reviewer")],
            [_review("reviewer", sha="before-edit")],
            False,
            "approved before-ed, before these waivers",
        ),
    ],
    ids=[
        "no-label",
        "author-labeled",
        "non-maintainer-labeled",
        "latest-labeler-wins",
        "labeler-never-reviewed",
        "labeler-requested-changes",
        "labeler-approval-dismissed",
        "only-author-approved",
        "label-without-labeled-event",
        "approval-without-commit",
        "labeler-approved-head",
        "comment-after-approval",
        "labeler-approved-same-waivers-before-rebase",
        "labeler-approved-before-waiver-edit",
    ],
)
def test_new_waiver_needs_label_and_approval_from_another_maintainer(
    approval, labels, timeline, reviews, approved, reason
) -> None:
    waivers = {"head": {PILLOW}, "rebased": {CERTIFI, PILLOW}, "before-edit": {CERTIFI}}
    decision = approval.floor_decision(
        author="author",
        labels=labels,
        new_waivers={PILLOW},
        maintainer_logins={"author", "reviewer"},
        timeline=timeline,
        reviews=reviews,
        waivers_at=waivers.__getitem__,
    )
    assert (decision.approved, reason in decision.reason) == (approved, True), decision.reason


@pytest.mark.parametrize(
    ("base", "head", "approved"),
    [
        (_pyproject(WAIVER, f"    {CERTIFI}"), _pyproject(WAIVER, f"    {CERTIFI}"), True),
        (_pyproject(f"    {CERTIFI}"), _pyproject(WAIVER, f"    {CERTIFI}"), False),
        (
            _pyproject(WAIVER, f"    {CERTIFI}", f"    {PILLOW}"),
            _pyproject(WAIVER, f"    {PILLOW}"),
            False,
        ),
        (
            _pyproject(WAIVER, f"    {CERTIFI}", f"    {PILLOW}"),
            _pyproject(f"    {PILLOW}", WAIVER, f"    {CERTIFI}"),
            True,
        ),
        (_pyproject(WAIVER, f"    {CERTIFI}"), _pyproject(f"    {CERTIFI}"), True),
    ],
    ids=["unchanged", "waiver-added", "deletion-moves-waiver", "reordered", "waiver-removed"],
)
def test_evaluate_compares_head_with_the_merge_base(approval, base, head, approved) -> None:
    decision = _evaluate(approval, _routes(base, head))
    assert decision.approved is approved, decision.reason


def _encoded(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode()}


def _routes(base: str, head: str, labels=(), timeline=(), reviews=()) -> dict:
    return {
        f"repos/{REPO}/pulls/7": {
            "head": {"sha": "head"},
            "base": {"sha": "main-tip"},
            "user": {"login": "author"},
            "labels": [{"name": name} for name in labels],
        },
        f"repos/{REPO}/compare/main-tip...head?per_page=1": {"merge_base_commit": {"sha": "fork"}},
        f"repos/{REPO}/contents/pyproject.toml?ref=head": _encoded(head),
        f"repos/{REPO}/contents/pyproject.toml?ref=fork": _encoded(base),
        f"repos/{REPO}/issues/7/timeline?per_page=100&page=1": list(timeline),
        f"repos/{REPO}/pulls/7/reviews?per_page=100&page=1": list(reviews),
    }


def _evaluate(approval, routes: dict, expected_head: str | None = None):
    return approval.evaluate(
        REPO,
        7,
        request=lambda args: routes[args[1]],
        maintainer_lookup=lambda repo: {"reviewer"},
        expected_head=expected_head,
    )


def test_evaluate_approves_through_the_reviewed_commit(approval) -> None:
    head = _pyproject(WAIVER, f"    {CERTIFI}")
    routes = _routes(
        _pyproject(f"    {CERTIFI}"),
        head,
        labels=LABEL,
        timeline=[_labeled("reviewer")],
        reviews=[_review("reviewer", sha="approved")],
    )
    routes[f"repos/{REPO}/contents/pyproject.toml?ref=approved"] = _encoded(head)
    decision = _evaluate(approval, routes, expected_head="head")
    assert decision.approved, decision.reason
    assert "approved by maintainer @reviewer at approved" in decision.reason


def test_evaluate_fails_when_the_head_moved_since_the_event(approval) -> None:
    routes = _routes(_pyproject(f"    {CERTIFI}"), _pyproject(f"    {CERTIFI}"))
    decision = _evaluate(approval, routes, expected_head="older")
    assert not decision.approved
    assert "PR head changed" in decision.reason


def test_rule_name_matches_the_lint_and_runs_in_the_required_check(approval) -> None:
    assert approval.RULE_NAME == lint.RULE_NAME
    job = yaml.safe_load((ROOT / ".github/workflows/maintainer-approval.yml").read_text())["jobs"][
        "approve"
    ]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] is False
    for source in [
        "dependency_floor_approval.py",
        "maintainer_approval.py",
        "dev/lint/_framework.py",
    ]:
        assert source in checkout["with"]["sparse-checkout"]
    step = next(s for s in job["steps"] if s.get("name") == "Require dependency floor approval")
    assert step["if"] == "${{ !cancelled() }}"
    assert step["env"]["PYTHONPATH"] == "${{ github.workspace }}"
    assert step["env"]["EXPECTED_HEAD"] == "${{ github.event.pull_request.head.sha }}"
    assert '--expected-head "$EXPECTED_HEAD"' in step["run"]
