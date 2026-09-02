from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import cast

import httpx
from omnigent_company_brain.adapters.common import AdaptedDocument, canonical_json
from omnigent_company_brain.encryption import CredentialCipher
from omnigent_company_brain.gbrain import GbrainSyncRunner
from omnigent_company_brain.models import BrainDocumentV1, sha256_text
from omnigent_company_brain.oauth import (
    OAuthStateCodec,
    OAuthToken,
    ProviderName,
    authorize_url,
    exchange_code,
    nonce_digest,
    refresh_google_token,
)
from omnigent_company_brain.oauth_config import resolve_oauth_client
from omnigent_company_brain.providers import (
    CompanyBrainProviderClient,
    ProviderFetchResult,
    ProviderRequestError,
    ProviderResource,
)
from omnigent_company_brain.publisher import (
    GitBrainPublisher,
    PublicationResult,
    RawObjectStore,
)

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import (
    IntegrationConnection,
    IntegrationSelection,
    IntegrationSyncRun,
)
from omnigent.stores.company_brain_store import CompanyBrainStore

_logger = logging.getLogger(__name__)


class CompanyBrainNotConfiguredError(RuntimeError):
    pass


def _safe_sync_error(stage: str, error: Exception) -> str:
    if isinstance(error, ProviderRequestError):
        return str(error)
    return {
        "setup": "Company brain setup failed. Check the operator configuration.",
        "fetch": "Source fetch failed. Reconnect the source or retry.",
        "publish": "Git publication failed. The sync is safe to retry.",
        "index": "Brain indexing failed. The Git commit is safe to retry.",
        "state": "Sync state update failed. The indexed commit is safe to retry.",
        "complete": "Sync completion failed. The indexed commit is safe to retry.",
    }[stage]


class CompanyBrainService:
    def __init__(
        self,
        *,
        store: CompanyBrainStore,
        credential_cipher: CredentialCipher,
        oauth_state_codec: OAuthStateCodec,
        repo_path: Path,
        gbrain_state_path: Path,
        repo_url: str | None = None,
        mcp_url: str | None = None,
        mcp_auth_ref: str | None = None,
        gbrain_executable: str = "gbrain",
        push_git: bool = False,
        no_embedding: bool = False,
        http_transport: httpx.AsyncBaseTransport | None = None,
        artifact_store: RawObjectStore | None = None,
    ) -> None:
        self._store = store
        self._credential_cipher = credential_cipher
        self._oauth_state_codec = oauth_state_codec
        self._repo_path = repo_path.resolve()
        self._gbrain_state_path = gbrain_state_path.resolve()
        self._repo_url = repo_url
        self._mcp_url = mcp_url
        self._mcp_auth_ref = mcp_auth_ref
        self._gbrain_executable = gbrain_executable
        self._push_git = push_git
        self._no_embedding = no_embedding
        self._http_transport = http_transport
        self._artifact_store = artifact_store
        self._sync_lock = asyncio.Lock()

    async def ensure_installation(self) -> None:
        existing = await asyncio.to_thread(self._store.get_installation)
        if existing is not None:
            return
        await asyncio.to_thread(
            partial(
                self._store.upsert_installation,
                uuid.uuid4().hex,
                repo_path=str(self._repo_path),
                repo_url=self._repo_url,
                gbrain_state_path=str(self._gbrain_state_path),
                mcp_url=self._mcp_url,
                mcp_auth_ref=self._mcp_auth_ref,
                status="ready",
            )
        )

    def begin_oauth(
        self,
        provider: ProviderName,
        *,
        admin_id: str,
        redirect_uri: str,
        return_to: str | None,
    ) -> dict[str, str]:
        client = resolve_oauth_client(provider)
        sealed, state = self._oauth_state_codec.seal(
            provider=provider,
            workspace_id=current_workspace_id(),
            admin_id=admin_id,
            redirect_uri=redirect_uri,
            return_to=return_to,
        )
        return {
            "authorize_url": authorize_url(
                provider,
                client=client,
                redirect_uri=redirect_uri,
                sealed_state=sealed,
                code_verifier=state.code_verifier,
            ),
            "state": sealed,
        }

    async def finish_oauth(
        self,
        provider: ProviderName,
        *,
        sealed_state: str,
        code: str,
        admin_id: str,
    ) -> IntegrationConnection:
        state = self._oauth_state_codec.open(sealed_state, expected_provider=provider)
        if state.workspace_id != current_workspace_id() or state.admin_id != admin_id:
            raise ValueError("OAuth state identity mismatch")
        claimed = await asyncio.to_thread(
            partial(
                self._store.claim_oauth_nonce,
                nonce_digest(state.nonce),
                expires_at=int(time.time()) + 600,
            )
        )
        if not claimed:
            raise ValueError("OAuth state was already used")
        client_config = resolve_oauth_client(provider)
        async with httpx.AsyncClient(
            transport=self._http_transport,
            timeout=60,
        ) as http_client:
            token = await exchange_code(
                provider,
                code=code,
                redirect_uri=state.redirect_uri,
                code_verifier=state.code_verifier,
                client_config=client_config,
                http_client=http_client,
            )
        connection_id = uuid.uuid4().hex
        encrypted = self._credential_cipher.encrypt_json(
            token.model_dump(mode="json"),
            workspace_id=current_workspace_id(),
            connection_id=connection_id,
        )
        return await asyncio.to_thread(
            partial(
                self._store.create_connection,
                connection_id,
                provider=provider,
                credential_ciphertext=encrypted,
                account_label=token.account_label,
                granted_scopes=token.granted_scopes,
                created_by=admin_id,
            )
        )

    async def discover_resources(self, connection_id: str) -> tuple[ProviderResource, ...]:
        connection, token = await self._connection_token(connection_id)
        provider = cast(ProviderName, connection.provider)
        async with httpx.AsyncClient(
            transport=self._http_transport,
            timeout=60,
        ) as http_client:
            return await CompanyBrainProviderClient(http_client).discover_resources(
                provider, token
            )

    async def preview_resources(
        self,
        connection_id: str,
        resources: tuple[ProviderResource, ...],
        *,
        limit: int = 5,
    ) -> tuple[BrainDocumentV1, ...]:
        connection, token = await self._connection_token(connection_id)
        provider = cast(ProviderName, connection.provider)
        documents: list[BrainDocumentV1] = []
        async with httpx.AsyncClient(
            transport=self._http_transport,
            timeout=60,
        ) as http_client:
            client = CompanyBrainProviderClient(http_client)
            for resource in resources:
                remaining = limit - len(documents)
                if remaining <= 0:
                    break
                result = await client.fetch_resource(
                    provider,
                    token,
                    resource,
                    connection_id=connection.id,
                    limit=remaining,
                )
                documents.extend(item.document for item in result.documents[:remaining])
        return tuple(documents)

    async def sync_selection(
        self,
        selection_id: str,
        *,
        trigger: str,
    ) -> IntegrationSyncRun:
        async with self._sync_lock:
            return await self._sync_selection_unlocked(selection_id, trigger=trigger)

    async def _sync_selection_unlocked(
        self,
        selection_id: str,
        *,
        trigger: str,
    ) -> IntegrationSyncRun:
        selection = await asyncio.to_thread(self._store.get_selection, selection_id)
        if selection is None:
            raise ValueError("selection not found")
        run = await asyncio.to_thread(
            partial(
                self._store.create_sync_run,
                uuid.uuid4().hex,
                connection_id=selection.connection_id,
                selection_id=selection.id,
                trigger=trigger,
            )
        )
        claimed = await asyncio.to_thread(self._store.claim_sync_run, run.id)
        if claimed is None:
            raise RuntimeError("sync run could not be claimed")
        stage = "setup"
        publication: PublicationResult | None = None
        try:
            await self.ensure_installation()
            stage = "fetch"
            connection, token = await self._connection_token(selection.connection_id)
            provider = cast(ProviderName, connection.provider)
            resource = ProviderResource(
                id=selection.external_resource_id,
                name=selection.resource_name,
                resource_type=selection.resource_type,
                source_url=selection.source_url,
                org_shared=selection.visibility_class == "org-shared",
                metadata={},
            )
            async with httpx.AsyncClient(
                transport=self._http_transport,
                timeout=120,
            ) as http_client:
                fetched = await CompanyBrainProviderClient(http_client).fetch_resource(
                    provider,
                    token,
                    resource,
                    connection_id=connection.id,
                )
            if not fetched.complete:
                raise RuntimeError("provider fetch did not complete")
            batch = [*fetched.documents, *self._deletions_for(selection, fetched)]
            if not batch:
                finished = await asyncio.to_thread(
                    partial(
                        self._store.finish_sync_run,
                        run.id,
                        status="succeeded",
                        fetched_count=0,
                        changed_count=0,
                        deleted_count=0,
                        skipped_count=0,
                        commit_sha=None,
                        gbrain_result=None,
                        error=None,
                    )
                )
                await asyncio.to_thread(
                    partial(
                        self._store.update_selection,
                        selection.id,
                        last_synced_at=int(time.time()),
                        page_count=0,
                        last_error=None,
                    )
                )
                if finished is None:
                    raise RuntimeError("sync run completion was lost")
                return finished
            stage = "publish"
            publication = await asyncio.to_thread(
                partial(
                    GitBrainPublisher(
                        self._repo_path,
                        push=self._push_git,
                        repo_url=self._repo_url,
                        raw_object_store=self._artifact_store,
                    ).publish,
                    batch,
                    sync_run_id=run.id,
                    complete_fetch=True,
                    selection_id=selection.id,
                )
            )
            stage = "index"
            receipt = await asyncio.to_thread(
                partial(
                    GbrainSyncRunner(
                        state_dir=self._gbrain_state_path,
                        executable=self._gbrain_executable,
                        no_embedding=self._no_embedding,
                    ).sync,
                    self._repo_path,
                    source_id="company-shared",
                    commit_sha=publication.commit_sha,
                )
            )
            stage = "state"
            await asyncio.to_thread(
                partial(
                    self._store.update_selection,
                    selection.id,
                    last_synced_at=int(time.time()),
                    page_count=len(fetched.documents),
                    last_error=None,
                )
            )
            stage = "complete"
            finished = await asyncio.to_thread(
                partial(
                    self._store.finish_sync_run,
                    run.id,
                    status="succeeded",
                    fetched_count=publication.fetched_count,
                    changed_count=publication.changed_count,
                    deleted_count=publication.deleted_count,
                    skipped_count=publication.skipped_count,
                    commit_sha=publication.commit_sha,
                    gbrain_result=receipt.result,
                    error=None,
                )
            )
            if finished is None:
                raise RuntimeError("sync run completion was lost")
            return finished
        except Exception as exc:
            _logger.warning(
                "company brain sync failed stage=%s error_type=%s",
                stage,
                type(exc).__name__,
            )
            safe_error = _safe_sync_error(stage, exc)
            finished = await asyncio.to_thread(
                partial(
                    self._store.finish_sync_run,
                    run.id,
                    status="failed",
                    fetched_count=publication.fetched_count if publication else 0,
                    changed_count=publication.changed_count if publication else 0,
                    deleted_count=publication.deleted_count if publication else 0,
                    skipped_count=publication.skipped_count if publication else 0,
                    commit_sha=publication.commit_sha if publication else None,
                    gbrain_result=None,
                    error=safe_error,
                )
            )
            await asyncio.to_thread(
                partial(
                    self._store.update_selection,
                    selection.id,
                    last_error=safe_error,
                )
            )
            if finished is None:
                raise
            return finished

    async def _connection_token(
        self,
        connection_id: str,
    ) -> tuple[IntegrationConnection, OAuthToken]:
        connection = await asyncio.to_thread(self._store.get_connection, connection_id)
        if connection is None:
            raise ValueError("connection not found")
        if connection.status != "connected" or not connection.credential_ciphertext:
            raise ValueError("connection needs reconnect")
        payload = self._credential_cipher.decrypt_json(
            connection.credential_ciphertext,
            workspace_id=current_workspace_id(),
            connection_id=connection.id,
        )
        oauth_credential = OAuthToken.model_validate(payload)
        if (
            connection.provider == "google"
            and oauth_credential.expires_at_ms is not None
            and oauth_credential.expires_at_ms <= int(time.time() * 1000) + 60_000
        ):
            async with httpx.AsyncClient(
                transport=self._http_transport,
                timeout=60,
            ) as http_client:
                oauth_credential = await refresh_google_token(
                    oauth_credential,
                    client_config=resolve_oauth_client("google"),
                    http_client=http_client,
                )
            encrypted = self._credential_cipher.encrypt_json(
                oauth_credential.model_dump(mode="json"),
                workspace_id=current_workspace_id(),
                connection_id=connection.id,
            )
            updated = await asyncio.to_thread(
                partial(
                    self._store.update_connection_credentials,
                    connection.id,
                    credential_ciphertext=encrypted,
                    granted_scopes=oauth_credential.granted_scopes,
                    account_label=oauth_credential.account_label,
                )
            )
            if updated is not None:
                connection = updated
        return connection, oauth_credential

    def _deletions_for(
        self,
        selection: IntegrationSelection,
        fetched: ProviderFetchResult,
    ) -> tuple[AdaptedDocument, ...]:
        manifest_path = self._repo_path / ".company-brain" / "documents.json"
        if not manifest_path.exists():
            return ()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current_paths = {item.document.stable_path for item in fetched.documents}
        deleted: list[AdaptedDocument] = []
        for stable_path, metadata in (manifest.get("documents") or {}).items():
            if metadata.get("selection_id") != selection.id or stable_path in current_paths:
                continue
            raw_json = canonical_json(
                {
                    "deleted": True,
                    "external_resource_id": metadata["external_resource_id"],
                    "selection_id": selection.id,
                }
            )
            raw_sha = sha256_text(raw_json)
            deleted.append(
                AdaptedDocument(
                    document=BrainDocumentV1(
                        provider=metadata["provider"],
                        connection_id=metadata["connection_id"],
                        external_resource_id=metadata["external_resource_id"],
                        stable_path=stable_path,
                        title=metadata["title"],
                        markdown="",
                        canonical_source_url=metadata["canonical_source_url"],
                        source_created_at=None,
                        source_modified_at=datetime.fromisoformat(metadata["source_modified_at"]),
                        content_sha256=sha256_text(""),
                        raw_object_reference=f".raw/{metadata['provider']}/{raw_sha}.json",
                        raw_sha256=raw_sha,
                        transform_schema_version="deletion.v1",
                        deletion_state="deleted",
                    ),
                    raw_json=raw_json,
                )
            )
        return tuple(deleted)
