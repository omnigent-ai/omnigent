from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnigent_company_brain.adapters.common import AdaptedDocument
from omnigent_company_brain.gbrain import GbrainSyncRunner
from omnigent_company_brain.models import (
    BrainDocumentV1,
    DeletionState,
    sha256_text,
    stable_document_path,
)
from omnigent_company_brain.publisher import GitBrainPublisher

_GBRAIN = shutil.which("gbrain")
_GBRAIN_POSTGRES_TEST_URL = os.environ.get("GBRAIN_POSTGRES_TEST_URL")


def _document(markdown: str, *, deletion_state: DeletionState = "active") -> AdaptedDocument:
    if deletion_state == "active":
        markdown = f'---\nsource_url: "https://www.notion.so/policy-1"\n---\n\n{markdown}'
    raw_json = '{"id":"policy-1","title":"Retention policy"}\n'
    return AdaptedDocument(
        document=BrainDocumentV1(
            provider="notion",
            connection_id="connection-1",
            external_resource_id="policy-1",
            stable_path=stable_document_path("notion", "connection-1", "policy-1"),
            title="Retention policy",
            markdown=markdown,
            canonical_source_url="https://www.notion.so/policy-1",
            source_created_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_modified_at=datetime(2026, 8, 26, tzinfo=UTC),
            content_sha256=sha256_text(markdown),
            raw_object_reference=f".raw/notion/{sha256_text(raw_json)}.json",
            raw_sha256=sha256_text(raw_json),
            transform_schema_version="notion-page.v1",
            deletion_state=deletion_state,
        ),
        raw_json=raw_json,
    )


def _gbrain(
    executable: str,
    state_dir: Path,
    repo: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *arguments],
        cwd=repo,
        env={**os.environ, "GBRAIN_HOME": str(state_dir)},
        check=check,
        capture_output=True,
        text=True,
    )


def _assert_git_to_gbrain_round_trip_and_soft_delete(tmp_path: Path) -> None:
    assert _GBRAIN is not None
    repo = tmp_path / "brain-repo"
    state_dir = tmp_path / "gbrain-state"
    publisher = GitBrainPublisher(repo)
    runner = GbrainSyncRunner(
        state_dir=state_dir,
        executable=_GBRAIN,
        no_embedding=True,
    )
    active = _document("# Retention policy\n\nPilot records are retained for exactly 42 days.\n")
    initial = publisher.publish([active], sync_run_id="run-1", complete_fetch=True)

    receipt = runner.sync(repo, source_id="company-shared", commit_sha=initial.commit_sha)
    replay = runner.sync(repo, source_id="company-shared", commit_sha=initial.commit_sha)
    search = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "search",
        "exactly 42 days",
        "--source-id",
        "company-shared",
    )
    active_slug = active.document.stable_path.removesuffix(".md")
    cited_page = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "get",
        active_slug,
        "--source-id",
        "company-shared",
    )

    assert receipt.replayed is False
    assert replay.replayed is True
    assert receipt.result["source_health"]["fresh"] is True
    assert receipt.result["source_health"]["queue_depth"] == 0
    assert active_slug in search.stdout
    assert "Retention policy" in search.stdout
    assert "https://www.notion.so/policy-1" in cited_page.stdout

    deleted_document = _document("", deletion_state="deleted")
    deleted = publisher.publish(
        [deleted_document],
        sync_run_id="run-2",
        complete_fetch=True,
    )
    runner.sync(repo, source_id="company-shared", commit_sha=deleted.commit_sha)
    slug = deleted_document.document.stable_path.removesuffix(".md")
    hidden = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "get",
        slug,
        "--source-id",
        "company-shared",
        check=False,
    )
    tombstone = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "get",
        slug,
        "--source-id",
        "company-shared",
        "--include-deleted",
    )
    deleted_search = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "search",
        "exactly 42 days",
        "--source-id",
        "company-shared",
    )

    assert hidden.returncode != 0 or "not found" in hidden.stdout.lower()
    assert slug not in deleted_search.stdout
    assert "Deleted: Retention policy" in tombstone.stdout
    assert "customer-owned Git history" in tombstone.stdout

    restored = publisher.publish(
        [active],
        sync_run_id="run-3",
        complete_fetch=True,
    )
    runner.sync(repo, source_id="company-shared", commit_sha=restored.commit_sha)
    restored_search = _gbrain(
        _GBRAIN,
        state_dir,
        repo,
        "search",
        "exactly 42 days",
        "--source-id",
        "company-shared",
    )

    assert slug in restored_search.stdout


@pytest.mark.skipif(_GBRAIN is None, reason="pinned gbrain binary is not installed")
def test_git_to_gbrain_round_trip_and_soft_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GBRAIN_DATABASE_URL", raising=False)
    _assert_git_to_gbrain_round_trip_and_soft_delete(tmp_path)


@pytest.mark.skipif(_GBRAIN is None, reason="pinned gbrain binary is not installed")
@pytest.mark.skipif(
    _GBRAIN_POSTGRES_TEST_URL is None,
    reason="GBRAIN_POSTGRES_TEST_URL is not configured",
)
def test_postgres_git_to_gbrain_round_trip_and_soft_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _GBRAIN_POSTGRES_TEST_URL is not None
    monkeypatch.setenv("GBRAIN_DATABASE_URL", _GBRAIN_POSTGRES_TEST_URL)
    _assert_git_to_gbrain_round_trip_and_soft_delete(tmp_path)
