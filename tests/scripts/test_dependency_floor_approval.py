"""Tests for ``.github/scripts/dependency_floor_approval.py``.

A young-dependency-floor waiver is a comment anyone can write, so the Maintainer
Approval job also requires a maintainer, the author included, to add the label
whenever a PR introduces a waiver its merge base does not have.
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


@pytest.mark.parametrize(
    ("labels", "timeline", "approved", "reason"),
    [
        (set(), [], False, "and add the `dependency-floor-approved` label"),
        (LABEL, [_labeled("author")], True, "approved by maintainer @author"),
        (LABEL, [_labeled("Reviewer")], True, "approved by maintainer @Reviewer"),
        (LABEL, [_labeled("outsider")], False, "non-maintainer @outsider"),
        (
            LABEL,
            [_labeled("reviewer"), _labeled("outsider"), _labeled("reviewer", "automerge")],
            False,
            "non-maintainer @outsider",
        ),
        (LABEL, [], False, "No `dependency-floor-approved` labeled event"),
    ],
    ids=[
        "no-label",
        "author-labeled",
        "other-maintainer-labeled",
        "non-maintainer-labeled",
        "latest-labeler-wins",
        "label-without-labeled-event",
    ],
)
def test_new_waiver_needs_the_label_from_a_maintainer(
    approval, labels, timeline, approved, reason
) -> None:
    decision = approval.floor_decision(
        labels=labels,
        new_waivers={PILLOW},
        maintainer_logins={"author", "reviewer"},
        timeline=timeline,
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


def _routes(base: str, head: str, labels=(), timeline=()) -> dict:
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
    }


def _evaluate(approval, routes: dict, expected_head: str | None = None):
    return approval.evaluate(
        REPO,
        7,
        request=lambda args: routes[args[1]],
        maintainer_lookup=lambda repo: {"reviewer"},
        expected_head=expected_head,
    )


def test_evaluate_approves_a_labeled_waiver(approval) -> None:
    routes = _routes(
        _pyproject(f"    {CERTIFI}"),
        _pyproject(WAIVER, f"    {CERTIFI}"),
        labels=LABEL,
        timeline=[_labeled("reviewer")],
    )
    decision = _evaluate(approval, routes, expected_head="head")
    assert decision.approved, decision.reason
    assert "approved by maintainer @reviewer" in decision.reason


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
