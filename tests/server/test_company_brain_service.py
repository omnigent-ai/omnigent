from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from omnigent_company_brain.encryption import CredentialCipher
from omnigent_company_brain.oauth import PROVIDERS, OAuthStateCodec, OAuthToken

from omnigent.server.company_brain_service import CompanyBrainService
from omnigent.stores.company_brain_store import UNSET
from omnigent.stores.company_brain_store.sqlalchemy_store import SqlAlchemyCompanyBrainStore

_KEY = "company-brain-test-key-material-with-adequate-length"


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def _notion_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/pages/page-1"):
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "page-1",
                "archived": False,
                "in_trash": False,
                "created_time": "2026-08-01T10:00:00Z",
                "last_edited_time": "2026-08-26T10:00:00Z",
                "url": "https://www.notion.so/page-1",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "Retention policy"}],
                    }
                },
            },
        )
    return httpx.Response(
        200,
        request=request,
        json={
            "results": [
                {
                    "id": "block-1",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {
                        "rich_text": [{"plain_text": "Retain pilot records for 42 days."}]
                    },
                }
            ],
            "has_more": False,
            "next_cursor": None,
        },
    )


def _setup(
    db_uri: str,
    tmp_path: Path,
    transport: httpx.AsyncBaseTransport,
) -> tuple[CompanyBrainService, SqlAlchemyCompanyBrainStore, str]:
    store = SqlAlchemyCompanyBrainStore(db_uri)
    cipher = CredentialCipher.from_material(_KEY)
    connection_id = _uid("service-connection")
    token = OAuthToken(access_token="notion-access-token", account_label="Policies")
    store.create_connection(
        connection_id,
        provider="notion",
        credential_ciphertext=cipher.encrypt_json(
            token.model_dump(mode="json"),
            workspace_id=0,
            connection_id=connection_id,
        ),
        account_label=token.account_label,
        granted_scopes=(),
        created_by="admin@example.com",
    )
    selection = store.create_selection(
        _uid("service-selection"),
        connection_id=connection_id,
        external_resource_id="page-1",
        resource_name="Retention policy",
        resource_type="notion_page",
        source_url="https://www.notion.so/page-1",
        transform_profile="notion-page.v1",
        rrule=None,
        timezone="UTC",
    )
    service = CompanyBrainService(
        store=store,
        credential_cipher=cipher,
        oauth_state_codec=OAuthStateCodec(_KEY),
        repo_path=tmp_path / "brain-repo",
        gbrain_state_path=tmp_path / "gbrain-state",
        http_transport=transport,
    )
    return service, store, selection.id


@pytest.mark.asyncio
async def test_provider_failure_records_run_without_mutating_git(
    db_uri: str,
    tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(400, request=request))
    service, store, selection_id = _setup(db_uri, tmp_path, transport)

    run = await service.sync_selection(selection_id, trigger="manual")

    assert run.status == "failed"
    assert run.commit_sha is None
    assert "provider request failed (400)" in (run.error or "")
    assert not (tmp_path / "brain-repo" / ".git").exists()
    selection = store.get_selection(selection_id)
    assert selection is not None
    assert selection.last_error == "provider request failed (400)"


@pytest.mark.asyncio
async def test_successful_fetch_commits_before_gbrain(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, selection_id = _setup(db_uri, tmp_path, httpx.MockTransport(_notion_handler))
    observed: list[str] = []

    class FakeGbrainSyncRunner:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def sync(self, repo_path: Path, *, source_id: str, commit_sha: str) -> SimpleNamespace:
            actual = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert actual == commit_sha
            assert list((repo_path / "sources" / "notion").glob("*.md"))
            observed.append(commit_sha)
            return SimpleNamespace(result={"status": "ok", "source_id": source_id})

    monkeypatch.setattr(
        "omnigent.server.company_brain_service.GbrainSyncRunner",
        FakeGbrainSyncRunner,
    )

    run = await service.sync_selection(selection_id, trigger="manual")

    assert run.status == "succeeded"
    assert run.commit_sha == observed[0]
    assert run.changed_count == 1
    assert run.deleted_count == 0
    selection = store.get_selection(selection_id)
    assert selection is not None
    assert selection.page_count == 1


@pytest.mark.asyncio
async def test_gbrain_failure_preserves_publication_lineage_and_safe_error(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, store, selection_id = _setup(
        db_uri,
        tmp_path,
        httpx.MockTransport(_notion_handler),
    )

    class FailingGbrainSyncRunner:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def sync(self, repo_path: Path, *, source_id: str, commit_sha: str) -> None:
            del repo_path, source_id, commit_sha
            raise RuntimeError("postgresql://user:secret@internal/brain")

    monkeypatch.setattr(
        "omnigent.server.company_brain_service.GbrainSyncRunner",
        FailingGbrainSyncRunner,
    )

    run = await service.sync_selection(selection_id, trigger="manual")

    assert run.status == "failed"
    assert run.commit_sha is not None
    assert run.changed_count == 1
    assert run.error == "Brain indexing failed. The Git commit is safe to retry."
    assert "secret" not in (run.error or "")
    assert "stage=index error_type=RuntimeError" in caplog.text
    assert "postgresql://user:secret" not in caplog.text
    selection = store.get_selection(selection_id)
    assert selection is not None
    assert selection.last_error == run.error


@pytest.mark.asyncio
async def test_state_update_failure_is_not_reported_as_index_failure(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, selection_id = _setup(
        db_uri,
        tmp_path,
        httpx.MockTransport(_notion_handler),
    )

    class FakeGbrainSyncRunner:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def sync(self, repo_path: Path, *, source_id: str, commit_sha: str) -> SimpleNamespace:
            del repo_path, source_id, commit_sha
            return SimpleNamespace(result={"source_health": {"fresh": True}})

    original_update = store.update_selection
    failed = False

    def fail_once(
        selection_id: str,
        *,
        state: str | None = None,
        rrule: str | None | Any = UNSET,
        timezone: str | None = None,
        last_synced_at: int | None = None,
        page_count: int | None = None,
        last_error: str | None = None,
    ) -> object:
        nonlocal failed
        if not failed and last_synced_at is not None:
            failed = True
            raise RuntimeError("database write failed")
        return original_update(
            selection_id,
            state=state,
            rrule=rrule,
            timezone=timezone,
            last_synced_at=last_synced_at,
            page_count=page_count,
            last_error=last_error,
        )

    monkeypatch.setattr(
        "omnigent.server.company_brain_service.GbrainSyncRunner",
        FakeGbrainSyncRunner,
    )
    monkeypatch.setattr(store, "update_selection", fail_once)

    run = await service.sync_selection(selection_id, trigger="manual")

    assert run.status == "failed"
    assert run.commit_sha is not None
    assert run.error == "Sync state update failed. The indexed commit is safe to retry."


@pytest.mark.asyncio
async def test_oauth_callback_is_admin_bound_and_single_use(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "access_token": "notion-access-token",
                "workspace_id": "workspace-1",
                "workspace_name": "Policies",
            },
        )

    monkeypatch.setenv("NOTION_OAUTH_CLIENT_ID", "notion-client")
    monkeypatch.setenv(PROVIDERS["notion"].client_credential_env, "notion-secret")
    service, _, _ = _setup(db_uri, tmp_path, httpx.MockTransport(handler))
    started = service.begin_oauth(
        "notion",
        admin_id="admin-one@example.com",
        redirect_uri="https://app.example.com/v1/company-brain/oauth/notion/callback",
        return_to="/settings/company-brain",
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        await service.finish_oauth(
            "notion",
            sealed_state=started["state"],
            code="code",
            admin_id="admin-two@example.com",
        )

    connection = await service.finish_oauth(
        "notion",
        sealed_state=started["state"],
        code="code",
        admin_id="admin-one@example.com",
    )
    assert connection.status == "connected"

    with pytest.raises(ValueError, match="already used"):
        await service.finish_oauth(
            "notion",
            sealed_state=started["state"],
            code="code",
            admin_id="admin-one@example.com",
        )
