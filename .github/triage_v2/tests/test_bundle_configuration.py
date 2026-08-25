from pathlib import Path

ROOT = Path(__file__).parents[1]


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
