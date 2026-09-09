from pathlib import Path

WORKFLOW = Path(".github/workflows/ui-preview.yml").read_text()


def test_cleanup_worker_requires_reconciler_identity_fields() -> None:
    for field in ("expected_id", "expected_create_time", "expected_update_time"):
        assert f"{field}:" in WORKFLOW
        assert f"inputs.{field}" in WORKFLOW


def test_cleanup_worker_revalidates_and_verifies_deletion() -> None:
    assert "App changed after cleanup review; refusing deletion" in WORKFLOW
    assert ".id == $id" in WORKFLOW
    assert ".create_time == $create_time" in WORKFLOW
    assert ".update_time == $update_time" in WORKFLOW
    assert "App still exists after deletion" in WORKFLOW


def test_dispatch_cleanup_preserves_workspace_source() -> None:
    assert 'if [[ "$DISPATCHED" != true && -n "$SOURCE_PATH" ]]' in WORKFLOW


def test_dispatch_cleanup_has_a_separate_concurrency_group() -> None:
    assert "format('cleanup-{0}', inputs.pr_number)" in WORKFLOW
