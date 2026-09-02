from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnigent_company_brain.adapters.common import AdaptedDocument, build_binary_document
from omnigent_company_brain.gbrain import GbrainSyncRunner
from omnigent_company_brain.models import (
    BrainDocumentV1,
    DeletionState,
    sha256_text,
    stable_document_path,
)
from omnigent_company_brain.publisher import (
    GitBrainPublisher,
    SecretInRawPayloadError,
    initialize_brain_repo,
)


def _document(
    markdown: str,
    *,
    deletion_state: DeletionState = "active",
    raw_json: str = '{"id":"page-1"}\n',
) -> AdaptedDocument:
    if deletion_state == "active":
        markdown = f'---\nsource_url: "https://www.notion.so/page-1"\n---\n\n{markdown}'
    path = stable_document_path("notion", "connection-1", "page-1")
    return AdaptedDocument(
        document=BrainDocumentV1(
            provider="notion",
            connection_id="connection-1",
            external_resource_id="page-1",
            stable_path=path,
            title="Pilot decision",
            markdown=markdown,
            canonical_source_url="https://www.notion.so/page-1",
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


def _rev_count(repo: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def test_publish_commit_noop_update_and_delete(tmp_path: Path) -> None:
    repo = tmp_path / "brain"
    publisher = GitBrainPublisher(repo)
    first_document = _document("# Pilot decision\n\nApproved.\n")

    initial = publisher.publish([first_document], sync_run_id="run-1", complete_fetch=True)
    replay = publisher.publish([first_document], sync_run_id="run-2", complete_fetch=True)
    updated = publisher.publish(
        [_document("# Pilot decision\n\nApproved with retention conditions.\n")],
        sync_run_id="run-3",
        complete_fetch=True,
    )
    deleted = publisher.publish(
        [_document("", deletion_state="deleted")],
        sync_run_id="run-4",
        complete_fetch=True,
    )

    assert initial.committed is True
    assert replay.committed is False
    assert replay.commit_sha == initial.commit_sha
    assert updated.committed is True
    assert deleted.deleted_count == 1
    assert _rev_count(repo) == 3
    assert not (repo / first_document.document.stable_path).exists()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


def test_partial_fetch_cannot_delete(tmp_path: Path) -> None:
    publisher = GitBrainPublisher(tmp_path / "brain")

    with pytest.raises(ValueError, match="partial fetches cannot publish deletions"):
        publisher.publish(
            [_document("", deletion_state="deleted")],
            sync_run_id="run-partial",
            complete_fetch=False,
        )


def test_secret_shaped_raw_payload_is_rejected_before_git(tmp_path: Path) -> None:
    publisher = GitBrainPublisher(tmp_path / "brain")
    raw_json = '{"access_token":"not-allowed","id":"page-1"}\n'

    with pytest.raises(SecretInRawPayloadError):
        publisher.publish(
            [_document("# Page\n", raw_json=raw_json)],
            sync_run_id="run-secret",
            complete_fetch=True,
        )


def test_gbrain_sync_is_keyed_by_source_and_commit(tmp_path: Path) -> None:
    repo = tmp_path / "brain"
    publication = GitBrainPublisher(repo).publish(
        [_document("# Page\n")],
        sync_run_id="run-1",
        complete_fetch=True,
    )
    commands: list[list[str]] = []

    def fake_runner(
        command: list[str],
        _cwd: Path,
        _env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:4] == ["sources", "list", "--json"]:
            stdout = "[]"
        elif command[1:4] == ["sources", "status", "--json"]:
            stdout = json.dumps(
                {
                    "schema_version": 1,
                    "sources": [
                        {
                            "source_id": "company-shared",
                            "total_pages": 1,
                            "total_chunks": 1,
                            "embedded_chunks": 0,
                            "embed_coverage_pct": 0,
                            "last_sync_at": "2026-08-29T10:00:00.000Z",
                            "lag_seconds": 0,
                            "failed_jobs_24h": 0,
                            "queue_depth": 0,
                            "sync_running": False,
                        }
                    ],
                }
            )
        elif command[1] == "sync":
            stdout = json.dumps({"schema_version": 1, "status": "ok"})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    runner = GbrainSyncRunner(
        state_dir=tmp_path / "gbrain",
        executable="gbrain",
        no_embedding=True,
        command_runner=fake_runner,
    )
    first = runner.sync(repo, source_id="company-shared", commit_sha=publication.commit_sha)
    replay = runner.sync(repo, source_id="company-shared", commit_sha=publication.commit_sha)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result
    assert first.result["source_health"]["fresh"] is True
    assert sum(command[1] == "sync" for command in commands) == 1
    assert sum(command[1:4] == ["sources", "status", "--json"] for command in commands) == 1


def test_gbrain_sync_initializes_configured_postgres(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "brain"
    publication = GitBrainPublisher(repo).publish(
        [_document("# Page\n")],
        sync_run_id="run-postgres",
        complete_fetch=True,
    )
    commands: list[list[str]] = []

    def fake_runner(
        command: list[str],
        _cwd: Path,
        _env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:4] == ["sources", "list", "--json"]:
            stdout = "[]"
        elif command[1:4] == ["sources", "status", "--json"]:
            stdout = json.dumps(
                {
                    "schema_version": 1,
                    "sources": [
                        {
                            "source_id": "company-shared",
                            "total_pages": 1,
                            "total_chunks": 1,
                            "embedded_chunks": 0,
                            "embed_coverage_pct": 0,
                            "last_sync_at": "2026-08-29T10:00:00.000Z",
                            "lag_seconds": 0,
                            "failed_jobs_24h": 0,
                            "queue_depth": 0,
                            "sync_running": False,
                        }
                    ],
                }
            )
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    database_url = "postgresql://gbrain:secret@gbrain-postgres:5432/gbrain"
    monkeypatch.setenv("GBRAIN_DATABASE_URL", database_url)
    runner = GbrainSyncRunner(
        state_dir=tmp_path / "gbrain",
        executable="gbrain",
        command_runner=fake_runner,
    )

    runner.sync(repo, source_id="company-shared", commit_sha=publication.commit_sha)

    assert [
        "gbrain",
        "init",
        "--url",
        database_url,
        "--non-interactive",
        "--json",
    ] in commands
    assert all("--pglite" not in command for command in commands)


def test_gbrain_sync_rejects_persisted_database_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "gbrain"
    config_dir = state_dir / ".gbrain"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "engine": "postgres",
                "database_url": "postgresql://gbrain:old-secret@db/gbrain",
                "embedding_disabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "GBRAIN_DATABASE_URL",
        "postgresql://gbrain:new-secret@db/gbrain",
    )
    runner = GbrainSyncRunner(state_dir=state_dir)

    with pytest.raises(ValueError, match="differs from runtime"):
        runner.initialize()


def test_gbrain_sync_does_not_record_an_unfresh_index(tmp_path: Path) -> None:
    repo = tmp_path / "brain"
    publication = GitBrainPublisher(repo).publish(
        [_document("# Page\n")],
        sync_run_id="run-unfresh",
        complete_fetch=True,
    )

    def fake_runner(
        command: list[str],
        _cwd: Path,
        _env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["sources", "list", "--json"]:
            stdout = "[]"
        elif command[1:4] == ["sources", "status", "--json"]:
            stdout = json.dumps(
                {
                    "schema_version": 1,
                    "sources": [
                        {
                            "source_id": "company-shared",
                            "last_sync_at": "2026-08-29T09:00:00.000Z",
                            "lag_seconds": 3600,
                            "queue_depth": 1,
                            "sync_running": True,
                        }
                    ],
                }
            )
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    state_dir = tmp_path / "gbrain"
    runner = GbrainSyncRunner(
        state_dir=state_dir,
        executable="gbrain",
        command_runner=fake_runner,
    )

    with pytest.raises(RuntimeError, match="not fresh"):
        runner.sync(repo, source_id="company-shared", commit_sha=publication.commit_sha)

    assert not (state_dir / "company-brain-sync-state.json").exists()


def test_noop_retry_pushes_commit_after_initial_push_failure(tmp_path: Path) -> None:
    repo = tmp_path / "brain"
    initialize_brain_repo(repo)
    missing_remote = tmp_path / "missing.git"
    subprocess.run(
        ["git", "remote", "add", "origin", str(missing_remote)],
        cwd=repo,
        check=True,
    )
    publisher = GitBrainPublisher(repo, push=True)
    document = _document("# Page\n")

    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish([document], sync_run_id="run-failed-push", complete_fetch=True)

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(remote)],
        cwd=repo,
        check=True,
    )
    retried = publisher.publish(
        [document],
        sync_run_id="run-retry-push",
        complete_fetch=True,
    )
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert retried.committed is False
    assert remote_head == retried.commit_sha


def test_publisher_clones_and_pushes_configured_customer_repository(tmp_path: Path) -> None:
    remote = tmp_path / "customer-brain.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "brain"

    published = GitBrainPublisher(repo, push=True, repo_url=str(remote)).publish(
        [_document("# Page\n")],
        sync_run_id="run-clone",
        complete_fetch=True,
    )
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert remote_head == published.commit_sha
    assert subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == str(remote)


def test_publisher_rejects_corrupt_existing_repository(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "brain"
    (repo / ".git").mkdir(parents=True)

    with pytest.raises(ValueError, match="valid Git repository"):
        GitBrainPublisher(repo).publish(
            [_document("# Page\n")],
            sync_run_id="run-corrupt",
            complete_fetch=True,
        )


def test_publisher_rejects_credentials_in_repository_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        GitBrainPublisher(
            tmp_path / "brain",
            repo_url="https://token@example.com/company/brain.git",
        )


def test_failed_commit_cleans_repository_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "brain"
    publisher = GitBrainPublisher(repo)
    original_git = publisher._git
    failed = False

    def fail_first_commit(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failed
        if "commit" in arguments and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, ["git", *arguments])
        return original_git(*arguments, check=check)

    monkeypatch.setattr(publisher, "_git", fail_first_commit)
    document = _document("# Page\n")

    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish([document], sync_run_id="run-failed", complete_fetch=True)

    assert original_git("status", "--porcelain").stdout == ""
    retried = publisher.publish(
        [document],
        sync_run_id="run-retried",
        complete_fetch=True,
    )
    assert retried.committed is True


def test_gbrain_mirror_reads_requested_commit_not_newer_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "brain"
    publisher = GitBrainPublisher(repo)
    first = publisher.publish(
        [_document("# Page\n\nFirst version.\n")],
        sync_run_id="run-first",
        complete_fetch=True,
    )
    publisher.publish(
        [_document("# Page\n\nNewer version.\n")],
        sync_run_id="run-newer",
        complete_fetch=True,
    )
    runner = GbrainSyncRunner(state_dir=tmp_path / "gbrain")

    mirror, _, _, _ = runner._materialize_source_mirror(repo, first.commit_sha)
    mirrored = (mirror / _document("# Page\n").document.stable_path).read_text()

    assert "First version." in mirrored
    assert "Newer version." not in mirrored


def test_binary_provenance_is_stored_outside_git_with_pointer(tmp_path: Path) -> None:
    class RawStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put(self, key: str, data: bytes) -> None:
            self.objects[key] = data

    raw_store = RawStore()
    content = b"binary workbook content"
    adapted = build_binary_document(
        provider="google",
        connection_id="connection-1",
        external_resource_id="workbook-1",
        title="Risk workbook",
        markdown=(
            '---\nsource_url: "https://drive.google.com/file/d/workbook-1/view"\n---\n\n'
            "# Risk workbook\n\nExtracted rows.\n"
        ),
        source_url="https://drive.google.com/file/d/workbook-1/view",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        modified_at=datetime(2026, 8, 26, tzinfo=UTC),
        raw_bytes=content,
        raw_metadata={"id": "workbook-1"},
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extension="xlsx",
        transform_schema_version="google-binary-markitdown.v1",
    )
    repo = tmp_path / "brain"

    GitBrainPublisher(repo, raw_object_store=raw_store).publish(
        [adapted],
        sync_run_id="run-binary",
        complete_fetch=True,
    )

    assert raw_store.objects == {adapted.raw_object_key: content}
    pointer = json.loads((repo / adapted.document.raw_object_reference).read_text())
    assert pointer["artifact_key"] == adapted.raw_object_key
    assert not (repo / "binary workbook content").exists()
