"""Unit tests for the UI-preview stale-app sweep selector.

The scheduled sweep workflow reclaims per-PR preview Databricks apps whose PR
is gone, closed, or no longer labelled ``ui-preview`` -- the reclamation path
that does not depend on each PR's single close event being delivered. The
selection logic lives in ``.github/scripts/ui-preview/sweep.py``; these tests
pin its safety properties: only per-PR preview apps are ever selected, an app
is selected only on positive evidence, and lookup failures keep the app while
surfacing an error.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "ui-preview" / "sweep.py"
spec = importlib.util.spec_from_file_location("ui_preview_sweep", SCRIPT)
assert spec and spec.loader
sweep = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sweep
spec.loader.exec_module(sweep)

REPO = "omnigent-ai/omnigent"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _pr(state: str, labels: list[str]) -> str:
    return json.dumps({"state": state, "labels": [{"name": name} for name in labels]})


def _gh_runner(responses: dict[int, subprocess.CompletedProcess]):
    """A fake command runner serving canned `gh api repos/.../pulls/<n>` replies."""

    def run(argv: list[str]) -> subprocess.CompletedProcess:
        assert argv[:2] == ["gh", "api"], argv
        number = int(argv[2].rsplit("/", 1)[1])
        return responses[number]

    return run


# --- preview_pr_number -------------------------------------------------------


def test_preview_pr_number_matches_per_pr_apps() -> None:
    assert sweep.preview_pr_number("omnigent-ui-preview-pr-6667") == 6667


@pytest.mark.parametrize(
    "name",
    [
        "omnigent-ui-preview-dev",  # main-branch preview: never swept
        "omnigent-repro-1234",  # other tenants in the shared workspace
        "omnigent-ui-preview-pr-",
        "omnigent-ui-preview-pr-12x",
        "prefix-omnigent-ui-preview-pr-12",
        "",
    ],
)
def test_preview_pr_number_rejects_other_apps(name: str) -> None:
    assert sweep.preview_pr_number(name) is None


# --- pr_needs_preview --------------------------------------------------------


def test_open_labelled_pr_still_needs_its_preview() -> None:
    run = _gh_runner({1: _proc(stdout=_pr("open", ["ui-preview", "bug"]))})
    assert sweep.pr_needs_preview(REPO, 1, run=run) is True


def test_closed_pr_no_longer_needs_its_preview() -> None:
    run = _gh_runner({1: _proc(stdout=_pr("closed", ["ui-preview"]))})
    assert sweep.pr_needs_preview(REPO, 1, run=run) is False


def test_open_unlabelled_pr_no_longer_needs_its_preview() -> None:
    run = _gh_runner({1: _proc(stdout=_pr("open", ["bug"]))})
    assert sweep.pr_needs_preview(REPO, 1, run=run) is False


def test_missing_pr_no_longer_needs_its_preview() -> None:
    run = _gh_runner({1: _proc(returncode=1, stderr="gh: Not Found (HTTP 404)")})
    assert sweep.pr_needs_preview(REPO, 1, run=run) is False


def test_transient_lookup_failure_raises_instead_of_selecting() -> None:
    run = _gh_runner({1: _proc(returncode=1, stderr="gh: Internal Server Error (HTTP 500)")})
    with pytest.raises(RuntimeError, match="PR #1 lookup failed"):
        sweep.pr_needs_preview(REPO, 1, run=run)


# --- list_apps ---------------------------------------------------------------


def test_list_apps_parses_a_json_array() -> None:
    apps = [{"name": "omnigent-ui-preview-pr-1"}]
    run = lambda argv: _proc(stdout=json.dumps(apps))  # noqa: E731
    assert sweep.list_apps(run=run) == apps


def test_list_apps_parses_a_wrapped_object() -> None:
    apps = [{"name": "omnigent-ui-preview-pr-1"}]
    run = lambda argv: _proc(stdout=json.dumps({"apps": apps}))  # noqa: E731
    assert sweep.list_apps(run=run) == apps


def test_list_apps_raises_on_cli_failure() -> None:
    run = lambda argv: _proc(returncode=1, stderr="Error: auth")  # noqa: E731
    with pytest.raises(RuntimeError, match="databricks apps list failed"):
        sweep.list_apps(run=run)


# --- select_stale_apps -------------------------------------------------------


def test_select_stale_apps_selects_only_on_positive_evidence() -> None:
    apps = [
        {"name": "omnigent-ui-preview-dev"},  # not per-PR: skipped, no lookup
        {"name": "omnigent-repro-777"},  # other tenant: skipped, no lookup
        {"name": "omnigent-ui-preview-pr-1"},  # open + labelled: kept
        {"name": "omnigent-ui-preview-pr-2"},  # closed: stale
        {"name": "omnigent-ui-preview-pr-3"},  # open, label removed: stale
        {"name": "omnigent-ui-preview-pr-4"},  # PR gone: stale
        {"name": "omnigent-ui-preview-pr-5"},  # lookup failed: kept + error
    ]
    run = _gh_runner(
        {
            1: _proc(stdout=_pr("open", ["ui-preview"])),
            2: _proc(stdout=_pr("closed", ["ui-preview"])),
            3: _proc(stdout=_pr("open", [])),
            4: _proc(returncode=1, stderr="gh: Not Found (HTTP 404)"),
            5: _proc(returncode=1, stderr="gh: bad gateway (HTTP 502)"),
        }
    )
    stale, errors = sweep.select_stale_apps(REPO, apps, run=run)
    assert stale == [
        "omnigent-ui-preview-pr-2",
        "omnigent-ui-preview-pr-3",
        "omnigent-ui-preview-pr-4",
    ]
    assert len(errors) == 1
    assert "omnigent-ui-preview-pr-5" in errors[0]


def test_select_stale_apps_with_no_preview_apps_selects_nothing() -> None:
    def run(argv: list[str]) -> subprocess.CompletedProcess:
        raise AssertionError(f"no lookup expected, got {argv}")

    stale, errors = sweep.select_stale_apps(REPO, [{"name": "omnigent-ui-preview-dev"}], run=run)
    assert stale == []
    assert errors == []


# --- sweep workflow wiring ---------------------------------------------------

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ui-preview-sweep.yml"


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_sweep_workflow_runs_on_a_schedule_and_on_demand() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True, {}))
    assert "schedule" in triggers, "sweep must not depend on per-PR events"
    assert "workflow_dispatch" in triggers, "quota crises need an on-demand sweep"


def test_sweep_workflow_deletes_via_the_selector_output() -> None:
    jobs = _workflow()["jobs"]
    text = yaml.safe_dump(jobs, default_flow_style=False)
    assert "sweep.py" in text
    assert "databricks apps delete" in text
    # The delete loop must refuse names outside the per-PR preview namespace.
    assert "omnigent-ui-preview-pr-*" in text
