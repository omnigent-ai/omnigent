"""Tests for ``.github/scripts/dependency_floor_approval.py``.

A young-dependency-floor waiver is self-attested, so the Maintainer Approval job
also requires the approval label from a maintainer other than the PR author
whenever a PR adds or changes a waiver line.
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
WAIVER = "    # custom-lint: disable-next=young-dependency-floor -- security fix, mirror checked"
PYPROJECT = "\n".join(
    [
        "[project]",
        "dependencies = [",
        '    "keyring>=24,<26",',
        WAIVER,
        '    "certifi>=2026.10.19",',
        '    "pillow>=12.2,<14",',
        "]",
        "",
    ]
)


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


def _patch(*lines: str, start: int = 2) -> str:
    return f"@@ -{start},5 +{start},5 @@\n" + "\n".join(lines)


@pytest.mark.parametrize(
    ("patch", "changed"),
    [
        (
            _patch(
                '     "keyring>=24,<26",', "+" + WAIVER, '+    "certifi>=2026.10.19",', start=3
            ),
            [4, 5],
        ),
        (
            _patch(
                " " + WAIVER, '-    "certifi>=2026.10.1",', '+    "certifi>=2026.10.19",', start=4
            ),
            [5],
        ),
        (
            _patch(
                " " + WAIVER,
                '     "certifi>=2026.10.19",',
                '-    "pillow>=13,<14",',
                '+    "pillow>=12.2,<14",',
                start=4,
            ),
            [],
        ),
    ],
    ids=["new-waiver", "floor-under-existing-waiver", "unrelated-line"],
)
def test_finds_waiver_lines_the_patch_adds_or_rewrites(approval, patch, changed) -> None:
    assert sorted(approval.waiver_lines(PYPROJECT) & approval.added_lines(patch)) == changed


def test_same_line_and_listed_rule_forms_count_as_waivers(approval) -> None:
    source = '"a>=1",  # custom-lint: disable=workspace-scoped-cache, young-dependency-floor\n'
    assert approval.waiver_lines(source) == {1}
    assert (
        approval.waiver_lines('"a>=1",  # custom-lint: disable=workspace-scoped-cache\n') == set()
    )


def _labeled(login: str, label: str = "dependency-floor-approved") -> dict:
    return {"event": "labeled", "actor": {"login": login}, "label": {"name": label}}


@pytest.mark.parametrize(
    ("labels", "timeline", "approved", "reason"),
    [
        (set(), [], False, "add the `dependency-floor-approved` label"),
        (
            {"dependency-floor-approved"},
            [_labeled("Reviewer")],
            True,
            "approved by maintainer @Reviewer",
        ),
        ({"dependency-floor-approved"}, [_labeled("author")], False, "added by the author"),
        ({"dependency-floor-approved"}, [_labeled("outsider")], False, "non-maintainer @outsider"),
        (
            {"dependency-floor-approved"},
            [_labeled("reviewer"), _labeled("author"), _labeled("reviewer", "automerge")],
            False,
            "added by the author",
        ),
    ],
    ids=["no-label", "other-maintainer", "author", "non-maintainer", "latest-labeler-wins"],
)
def test_waiver_needs_label_from_another_maintainer(
    approval, labels, timeline, approved, reason
) -> None:
    decision = approval.floor_decision(
        author="author",
        labels=labels,
        changed_waivers=[4, 5],
        maintainer_logins={"author", "reviewer"},
        timeline=timeline,
    )
    assert decision.approved is approved
    assert reason in decision.reason


@pytest.mark.parametrize(
    ("files", "approved"),
    [
        ([{"filename": "README.md", "patch": "@@ -1 +1 @@\n+x"}], True),
        ([{"filename": "pyproject.toml", "patch": _patch("+" + WAIVER, start=4)}], False),
        ([{"filename": "pyproject.toml"}], False),
    ],
    ids=["pyproject-unchanged", "waiver-added", "patch-omitted"],
)
def test_evaluate_reads_the_pull_request_through_the_api(approval, files, approved) -> None:
    routes = {
        f"repos/{REPO}/pulls/7": {
            "head": {"sha": "abc"},
            "user": {"login": "author"},
            "labels": [],
        },
        f"repos/{REPO}/pulls/7/files?per_page=100&page=1": files,
        f"repos/{REPO}/contents/pyproject.toml?ref=abc": {
            "content": base64.b64encode(PYPROJECT.encode()).decode()
        },
        f"repos/{REPO}/issues/7/timeline?per_page=100&page=1": [],
    }
    decision = approval.evaluate(
        REPO, 7, request=lambda args: routes[args[1]], maintainer_lookup=lambda repo: {"reviewer"}
    )
    assert decision.approved is approved


def test_rule_name_matches_the_lint_and_runs_in_the_required_check(approval) -> None:
    assert approval.RULE_NAME == lint.RULE_NAME
    job = yaml.safe_load((ROOT / ".github/workflows/maintainer-approval.yml").read_text())["jobs"][
        "approve"
    ]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "main"
    for source in [
        "dependency_floor_approval.py",
        "maintainer_approval.py",
        "dev/lint/_framework.py",
    ]:
        assert source in checkout["with"]["sparse-checkout"]
    step = next(s for s in job["steps"] if s.get("name") == "Require dependency floor approval")
    assert step["if"] == "${{ !cancelled() }}"
    assert step["env"]["PYTHONPATH"] == "${{ github.workspace }}"
