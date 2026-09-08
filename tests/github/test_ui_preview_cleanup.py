"""Regression tests for UI Preview Databricks app cleanup.

The UI Preview workflow deploys one Databricks app per labelled PR
(``omnigent-ui-preview-pr-<N>``) into a workspace capped at 100 apps. Deploys
for new PRs fail live with::

    Error: Failed to create app omnigent-ui-preview-pr-6667. Workspace
    3272836215725701 has reached the maximum limit of 100 apps.

The close-event cleanup job has existed since the workflow's first commit and
runs green on close events, yet stale apps still accumulated to the quota:
close-time-only cleanup leaks an app slot forever whenever a delete is missed
or silently skipped (its ``databricks apps get`` guard swallows auth and
transient failures as "app absent"), and open PRs plus other tenants also hold
slots in the shared workspace. The durable fix must add a reclamation path
that does not depend on each PR's single close event being delivered and
handled: a schedule-triggered sweep of stale preview apps, or pruning inside
the deploy path before app creation.

``test_stale_preview_apps_are_reclaimed_without_a_close_event`` is keyed to
that failure: it fails while no such reclamation mechanism exists and passes
once one lands. ``test_close_event_cleanup_still_present`` guards the existing
close-time cleanup against regression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
UI_PREVIEW = WORKFLOWS_DIR / "ui-preview.yml"


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} did not parse to a mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare `on:` key as boolean True.
    raw = workflow.get("on", workflow.get(True, {}))
    return raw if isinstance(raw, dict) else {}


def _jobs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [job for job in jobs.values() if isinstance(job, dict)]


def _searchable(job: dict[str, Any]) -> str:
    """The job re-serialized, so run scripts, env values, and names all match."""
    return yaml.safe_dump(job, default_flow_style=False)


def test_close_event_cleanup_still_present() -> None:
    """Per-PR close-event cleanup (delete the PR's preview app) must stay."""
    workflow = _load(UI_PREVIEW)

    pull_request_target = _triggers(workflow).get("pull_request_target") or {}
    types = pull_request_target.get("types") or []
    assert "closed" in types, (
        "ui-preview.yml no longer listens for pull_request_target 'closed' "
        "events, so per-PR preview apps would never be deleted on close"
    )

    cleanup_jobs = [
        job
        for job in _jobs(workflow)
        if "closed" in str(job.get("if", "")) and "databricks apps delete" in _searchable(job)
    ]
    assert cleanup_jobs, (
        "ui-preview.yml has no job that runs on PR close and deletes the "
        "PR's Databricks preview app via 'databricks apps delete'"
    )


def _is_scheduled_sweep(workflow: dict[str, Any]) -> bool:
    """A schedule-triggered workflow that deletes stale preview apps."""
    if "schedule" not in _triggers(workflow):
        return False
    text = "\n".join(_searchable(job) for job in _jobs(workflow))
    return "databricks apps delete" in text and "omnigent-ui-preview" in text


def _deploy_prunes_before_create(workflow: dict[str, Any]) -> bool:
    """The app-creating job itself reclaims stale preview apps at quota."""
    for job in _jobs(workflow):
        text = _searchable(job)
        if "databricks apps create" not in text:
            continue
        if "databricks apps delete" in text and "databricks apps list" in text:
            return True
    return False


def test_stale_preview_apps_are_reclaimed_without_a_close_event() -> None:
    """Quota reclamation must not depend on per-PR close events.

    Live failure: 'Error: Failed to create app omnigent-ui-preview-pr-<N>.
    Workspace 3272836215725701 has reached the maximum limit of 100 apps.'
    Stale ``omnigent-ui-preview-pr-*`` apps accumulated even though the
    close-event cleanup exists and passes, because any missed or silently
    skipped delete leaks an app slot with nothing to reclaim it. Require a
    reclamation mechanism independent of close-event delivery: a scheduled
    sweep workflow, or pruning inside the deploy path before app creation.
    """
    workflow_paths = sorted(list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml")))
    workflows = [_load(path) for path in workflow_paths]

    has_scheduled_sweep = any(_is_scheduled_sweep(wf) for wf in workflows)
    has_deploy_time_pruning = _deploy_prunes_before_create(_load(UI_PREVIEW))

    assert has_scheduled_sweep or has_deploy_time_pruning, (
        "No mechanism reclaims stale omnigent-ui-preview-pr-* Databricks apps "
        "besides the per-PR close-event cleanup, so any missed delete leaks "
        "an app slot until the shared workspace hits its 100-app cap and "
        "every new preview deploy fails ('has reached the maximum limit of "
        "100 apps'). Add a schedule-triggered sweep workflow that deletes "
        "stale preview apps, or prune stale apps in the deploy job before "
        "'databricks apps create'."
    )
