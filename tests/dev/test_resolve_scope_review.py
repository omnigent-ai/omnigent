from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "resolve_scope_review", ROOT / "dev/resolve-agent/scope_review.py"
)
assert SPEC and SPEC.loader
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


@pytest.fixture
def snapshot() -> dict:
    return {
        "version": 1,
        "repo": "example/project",
        "pr": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "base_branch": "main",
        "title": "Display model names in the picker",
        "body": "Fixes #5",
        "issues": [
            {
                "url": "https://github.com/example/project/issues/5",
                "title": "Model picker shows raw IDs",
                "body": "Show readable names and fall back to the ID when a name is missing.",
            }
        ],
        "files": ["server/catalog.py", "tests/test_catalog.py", "web/picker.ts"],
    }


@pytest.fixture
def report(snapshot: dict) -> dict:
    return {
        "version": 1,
        "context_digest": scope.digest(snapshot),
        "scope": {
            "problem": "Model picker displays raw catalog IDs",
            "acceptance_criteria": ["Readable names appear; unnamed models retain their IDs"],
            "exclusions": ["Favorites and sorting"],
            "status": "clear",
            "reason": "API and picker changes jointly restore readable labels",
        },
        "files": [
            {
                "path": path,
                "changes": [
                    {
                        "classification": "necessary",
                        "location": "label handling",
                        "reason": reason,
                    }
                ],
            }
            for path, reason in zip(
                snapshot["files"],
                [
                    "Expose display names through the catalog API",
                    "Verify missing-name fallback and readable-name behavior",
                    "Render the supplied display name",
                ],
                strict=True,
            )
        ],
    }


def test_supporting_changes_across_layers_pass(snapshot: dict, report: dict) -> None:
    assert scope.verdict(report, snapshot)[0]


@pytest.mark.parametrize("classification", ["unrelated", "uncertain", "typo", None])
def test_extra_change_in_relevant_file_blocks(
    snapshot: dict, report: dict, classification: str | None
) -> None:
    report["files"][-1]["changes"].append(
        {
            "classification": classification,
            "location": "favorites button",
            "reason": "Favorites are independent of fixing display names",
        }
    )
    assert not scope.verdict(report, snapshot)[0]


@pytest.mark.parametrize("mutation", ["head", "base", "retarget", "body", "issue", "files"])
def test_changed_review_inputs_invalidate_pass(
    snapshot: dict, report: dict, mutation: str
) -> None:
    if mutation in {"head", "base"}:
        snapshot[f"{mutation}_sha"] = "c" * 40
    elif mutation == "retarget":
        snapshot["base_branch"] = "release"
    elif mutation == "body":
        snapshot["body"] += " Also add favorites."
    elif mutation == "issue":
        snapshot["issues"][0]["body"] += " Add favorites too."
    else:
        snapshot["files"].append("web/favorites.ts")
    assert not scope.verdict(report, snapshot)[0]


@pytest.mark.parametrize(
    "body",
    [
        "Fixes #5",
        "Part of #5",
        "Refs example/project#5",
        "Related to https://github.com/example/project/issues/5",
    ],
)
def test_context_reads_source_issue_and_uses_stable_diff_base(
    monkeypatch: pytest.MonkeyPatch, snapshot: dict, body: str
) -> None:
    pr = {
        "head": {"sha": snapshot["head_sha"]},
        "base": {"sha": "d" * 40, "ref": "main"},
        "title": snapshot["title"],
        "body": body,
        "changed_files": 3,
    }
    issue = snapshot["issues"][0]
    monkeypatch.setattr(
        scope, "pages", lambda *args: [{"filename": path} for path in snapshot["files"]]
    )
    reads = []

    def api(endpoint: str, payload=None, **kwargs):
        reads.append(endpoint)
        if "/pulls/" in endpoint:
            return deepcopy(pr)
        if "/compare/" in endpoint:
            return {"merge_base_commit": {"sha": snapshot["base_sha"]}}
        if endpoint == "graphql":
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "closingIssuesReferences": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        assert endpoint == "repos/example/project/issues/5"
        return {**issue, "html_url": issue["url"]}

    monkeypatch.setattr(scope, "api", api)
    before = scope.context("example/project", 7)
    assert before["issues"] == [issue]
    assert "repos/example/project/issues/5" in reads
    pr["base"]["sha"] = "e" * 40
    assert scope.context("example/project", 7) == before
    pr["changed_files"] = 4
    with pytest.raises(ValueError, match="Incomplete"):
        scope.context("example/project", 7)


def test_pagination_reads_all_changed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scope,
        "api",
        lambda endpoint: (
            [{"filename": str(n)} for n in range(100)]
            if endpoint.endswith("page=1")
            else [{"filename": "last.py"}]
        ),
    )
    assert len(scope.pages("repos/example/project/pulls/7/files")) == 101


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-file",
        "duplicate-file",
        "unknown-file",
        "missing-changes",
        "no-reason",
        "no-location",
        "no-criteria",
        "no-exclusions",
        "ambiguous-problem",
        "not-object",
        "empty-files",
    ],
)
def test_incomplete_assessment_blocks(snapshot: dict, report: dict, mutation: str) -> None:
    if mutation == "missing-file":
        report["files"].pop()
    elif mutation == "duplicate-file":
        report["files"].append(deepcopy(report["files"][0]))
    elif mutation == "unknown-file":
        report["files"][0]["path"] = "other.py"
    elif mutation == "missing-changes":
        report["files"][0]["changes"] = []
    elif mutation in {"no-reason", "no-location"}:
        del report["files"][0]["changes"][0][mutation.removeprefix("no-")]
    elif mutation in {"no-criteria", "no-exclusions"}:
        report["scope"]["acceptance_criteria" if mutation == "no-criteria" else "exclusions"] = []
    elif mutation == "ambiguous-problem":
        report["scope"]["status"] = "uncertain"
    elif mutation == "not-object":
        report["files"][0] = "all good"
    else:
        report["files"] = []
    assert not scope.verdict(report, snapshot)[0]


def test_structured_output_survives_prose_and_rejects_ambiguity(report: dict) -> None:
    text = f"## Scope\n{scope.START}\n{json.dumps(report)}\n{scope.END}\nOther findings.\n"
    assert scope.assessment(text) == report
    for invalid in [
        "Everything is fine",
        text + text,
        text.replace(scope.END, ""),
        f"{scope.START}\n[]\n{scope.END}",
    ]:
        with pytest.raises(ValueError):
            scope.assessment(invalid)


@pytest.mark.parametrize("classification", ["necessary", "unrelated"])
def test_render_collapses_data_without_changing_verdict(
    snapshot: dict, report: dict, classification: str
) -> None:
    report["files"][0]["changes"][0]["classification"] = classification
    report["scope"]["reason"] = "Text with ``` and </details> stays inside the JSON string."
    summary = "## Summary\nRemove the independent feature.\n\n"
    footer = "\n\nReview footer."
    raw = f"{summary}{scope.START}\n{json.dumps(report)}\n{scope.END}{footer}"
    rendered = scope.render_review(raw)
    assert rendered.startswith(summary + "<details>\n")
    assert "<summary>Scope assessment data</summary>\n\n" in rendered
    assert f"{scope.START}\n```json\n" in rendered
    assert rendered.endswith("</details>" + footer)
    assert scope.assessment(rendered) == report
    assert scope.verdict(scope.assessment(rendered), snapshot) == scope.verdict(report, snapshot)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("classification", ["necessary", "unrelated", "uncertain"])
def test_comment_step_formats_scope_only_when_requested(
    tmp_path: Path, report: dict, enabled: bool, classification: str
) -> None:
    report["files"][0]["changes"][0]["classification"] = classification
    workflow = yaml.safe_load((ROOT / ".github/workflows/polly-review.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["review"]["steps"]
        if step["name"] == "Post review comment"
    )
    raw = f"## Summary\nReview text.\n{scope.START}\n{json.dumps(report)}\n{scope.END}"
    comment = tmp_path / "comment.md"
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    subprocess.run(
        ["bash", "-c", step["run"].replace("/tmp/comment.md", str(comment))],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "REVIEW_TEXT": raw,
            "RESOLVE_SCOPE": str(enabled).lower(),
            "HEAD_SHA": "a" * 40,
            "REPO": "example/project",
            "PR_NUMBER": "7",
            "RUN_URL": "https://example.test/run",
        },
        check=True,
        capture_output=True,
        timeout=10,
    )
    posted = comment.read_text()
    assert ("<details>" in posted) is enabled
    assert scope.assessment(posted) == report
    assert (scope.render_review(raw) if enabled else raw) in posted


@pytest.mark.parametrize("case", ["necessary", "unrelated", "missing", "changed"])
def test_read_only_check_requires_a_current_passing_assessment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot: dict, report: dict, case: str
) -> None:
    if case == "unrelated":
        report["files"][0]["changes"][0]["classification"] = "unrelated"
    review = tmp_path / "review.txt"
    review.write_text(
        "Looks fine"
        if case == "missing"
        else f"{scope.START}\n{json.dumps(report)}\n{scope.END}\n"
    )
    later = deepcopy(snapshot)
    if case == "changed":
        later["issues"][0]["body"] += " Also add favorites."
    reads = iter([snapshot, later])
    monkeypatch.setattr(scope, "context", lambda *args: next(reads))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scope_review.py",
            "check",
            "--repo",
            "example/project",
            "--pr",
            "7",
            "--review",
            str(review),
        ],
    )
    assert scope.main() == (0 if case == "necessary" else 1)


@pytest.mark.parametrize("complete", [True, False])
def test_prepare_rejects_partial_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot: dict, complete: bool
) -> None:
    diff = tmp_path / "diff.txt"
    paths = snapshot["files"] if complete else snapshot["files"][:-1]
    diff.write_text("".join(f"diff --git a/{path} b/{path}\n" for path in paths))
    context_path = tmp_path / "context.json"
    monkeypatch.setattr(scope, "context", lambda *args: snapshot)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scope_review.py",
            "prepare",
            "--repo",
            "example/project",
            "--pr",
            "7",
            "--diff",
            str(diff),
            "--context",
            str(context_path),
        ],
    )
    assert scope.main() == (0 if complete else 1)
    assert context_path.exists() is complete


@pytest.mark.parametrize("enabled", [True, False])
def test_scope_review_is_opt_in_and_bypasses_old_review_deduplication(
    tmp_path: Path, enabled: bool
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/polly-review.yml").read_text())
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["resolve_scope"]["default"] is False
    steps = workflow["jobs"]["review"]["steps"]
    assert all("scope_review.py check" not in step.get("run", "") for step in steps)
    dupe = next(step for step in steps if step.get("id") == "dupe")
    gh = tmp_path / "gh"
    gh.write_text("""#!/bin/sh
review_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
case "$*" in
  *'/pulls/'*) echo "$review_sha" ;;
  *'/issues/'*)
    printf '%s\\t<!-- polly-reviewed-sha: %s -->\\n' https://example.test/review "$review_sha" ;;
esac
""")
    gh.chmod(0o755)
    script = dupe["run"].replace("/tmp/pr_comments.tsv", str(tmp_path / "comments.tsv"))
    script = script.replace("/tmp/skip_comment.md", str(tmp_path / "comment.md"))
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "REPO": "example/project",
            "PR_NUMBER": "7",
            "COMMENT_BODY": "",
            "GITHUB_OUTPUT": str(output),
            "RESOLVE_SCOPE": str(enabled).lower(),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if enabled:
        assert not output.exists()
    else:
        assert "duplicate=true" in output.read_text()
