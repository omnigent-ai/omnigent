from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT.parent / "workflows"


def test_trigger_waits_for_bronze_table_updates_and_is_safe_by_default() -> None:
    bundle = (ROOT / "databricks.yml").read_text()
    job = (ROOT / "resources/issue_prioritization.job.yml").read_text()

    assert "schedule_pause_status:\n" in bundle
    assert "default: PAUSED" in bundle
    assert "scheduled_mode:\n" in bundle
    assert "default: dry_run" in bundle
    assert "pause_status: ${var.schedule_pause_status}" in job
    assert "table_update:" in job
    assert "${var.catalog}.${var.schema}.${var.source_table}" in job
    assert "default: ${var.scheduled_mode}" in job


def test_job_passes_configured_github_app_secret_keys() -> None:
    job = (ROOT / "resources/issue_prioritization.job.yml").read_text()

    assert "github-auth-mode: ${var.github_auth_mode}" in job
    assert "github-app-client-id-secret-key: ${var.github_app_client_id_secret_key}" in job
    assert "github-app-private-key-secret-key: ${var.github_app_private_key_secret_key}" in job


def test_github_events_share_one_v2_workflow() -> None:
    intake = (WORKFLOWS / "issue-triage.yml").read_text()
    response = (WORKFLOWS / "needs-info-response.yml").read_text()
    reusable = (WORKFLOWS / "issue-prioritization-v2.yml").read_text()

    assert "uses: ./.github/workflows/issue-prioritization-v2.yml" in intake
    assert "uses: ./.github/workflows/issue-prioritization-v2.yml" in response
    assert "workflow_call:" in reusable
    assert "--remove-label needs-info" not in response
    assert "reopen_closed:" in response
    assert "reopen_closed: true" in response
    assert "group: issue-prioritization-v2-${{ inputs.issue_number }}" in reusable
    assert "  prioritize:\n    if: vars.ISSUE_PRIORITIZATION_V2_ENABLED" not in reusable
    assert reusable.count("if: vars.ISSUE_PRIORITIZATION_V2_ENABLED == 'true'") == 3
    assert "if: always() && vars.ISSUE_PRIORITIZATION_V2_ENABLED == 'true'" in reusable


def test_v2_still_runs_when_legacy_intake_fails() -> None:
    intake = (WORKFLOWS / "issue-triage.yml").read_text()
    prioritize = intake.split("  prioritize-v2:", 1)[1]

    assert "needs.triage.result == 'success'" not in prioritize


def test_v2_owns_intake_when_enabled_and_manual_dispatch_is_dry_by_default() -> None:
    intake = (WORKFLOWS / "issue-triage.yml").read_text()
    reusable = (WORKFLOWS / "issue-prioritization-v2.yml").read_text()
    legacy = intake.split("  triage:", 1)[1].split("  prioritize-v2:", 1)[0]
    prioritize = intake.split("  prioritize-v2:", 1)[1]

    assert "vars.ISSUE_PRIORITIZATION_V2_ENABLED != 'true'" in legacy
    assert "remove in 0.12.0" in legacy
    assert "cancel-in-progress: false" in intake
    assert "github.event.action == 'opened'" in prioritize
    assert (
        "issue_number: ${{ fromJSON(format('{0}', "
        "github.event.issue.number || inputs.issue_number)) }}" in prioritize
    )
    apply_expression = (
        "apply: ${{ github.event_name != 'workflow_dispatch' || inputs.apply_labels }}"
    )
    assert apply_expression in prioritize
    assert "--intake --maintainers .github/MAINTAINER" in reusable
    assert "mode=dry_run" in reusable


def test_needs_info_expiry_is_gated_and_previewable() -> None:
    workflow = (WORKFLOWS / "needs-info-expiry.yml").read_text()

    assert "ISSUE_TRIAGE_CLOSE_NEEDS_INFO" in workflow
    assert "ISSUE_PRIORITIZATION_V2_ENABLED" in workflow
    assert 'if [ "$V2_ENABLED" != "true" ]' in workflow
    assert "workflow_dispatch:" in workflow
    assert "--apply" in workflow
