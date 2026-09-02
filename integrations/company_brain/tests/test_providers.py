from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from omnigent_company_brain.oauth import OAuthToken
from omnigent_company_brain.providers import CompanyBrainProviderClient, ProviderResource
from openpyxl import Workbook


@pytest.mark.asyncio
async def test_slack_discovery_paginates_and_retries_rate_limit() -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        cursor = request.url.params.get("cursor")
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        if cursor == "next":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C2", "name": "product", "is_private": False}],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "security", "is_private": False},
                    {"id": "G1", "name": "leadership", "is_private": True},
                ],
                "response_metadata": {"next_cursor": "next"},
            },
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resources = await CompanyBrainProviderClient(http_client, sleep=sleep).discover_resources(
            "slack", OAuthToken(access_token="token")
        )

    assert [resource.id for resource in resources] == ["C1", "C2"]
    assert all(resource.org_shared for resource in resources)
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_google_preview_is_bounded_and_incomplete() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures/google_document.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drives/drive-1"):
            return httpx.Response(200, json={"id": "drive-1", "name": "Shared policies"})
        if request.url.path.endswith("/files"):
            files = [
                {**fixture["file"], "id": f"doc-{index}", "name": f"Document {index}"}
                for index in range(5)
            ]
            return httpx.Response(200, json={"files": files, "nextPageToken": "more"})
        return httpx.Response(200, text=str(fixture["exported_markdown"]))

    resource = ProviderResource(
        id="drive-1",
        name="Shared policies",
        resource_type="google_shared_drive",
        source_url="https://drive.google.com/drive/folders/drive-1",
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await CompanyBrainProviderClient(http_client).fetch_resource(
            "google",
            OAuthToken(access_token="token"),
            resource,
            connection_id="connection-1",
            limit=3,
        )

    assert len(result.documents) == 3
    assert result.complete is False


@pytest.mark.asyncio
async def test_google_binary_file_is_downloaded_and_converted() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Risk", "Owner"])
    sheet.append(["Privacy", "Ada"])
    content = BytesIO()
    workbook.save(content)
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=content.getvalue())
        return httpx.Response(
            200,
            json={
                "id": "xlsx-1",
                "name": "Risk workbook.xlsx",
                "mimeType": media_type,
                "createdTime": "2026-08-01T10:00:00Z",
                "modifiedTime": "2026-08-26T10:00:00Z",
                "webViewLink": "https://drive.google.com/file/d/xlsx-1/view",
                "trashed": False,
                "driveId": "drive-1",
                "size": len(content.getvalue()),
            },
        )

    resource = ProviderResource(
        id="xlsx-1",
        name="Risk workbook.xlsx",
        resource_type="google_binary_file",
        source_url="https://drive.google.com/file/d/xlsx-1/view",
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await CompanyBrainProviderClient(http_client).fetch_resource(
            "google",
            OAuthToken(access_token="token"),
            resource,
            connection_id="connection-1",
        )

    assert result.complete is True
    assert result.documents[0].raw_bytes == content.getvalue()
    assert "| Privacy | Ada |" in result.documents[0].document.markdown


@pytest.mark.asyncio
async def test_private_resource_is_rejected_before_provider_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    resource = ProviderResource(
        id="G1",
        name="#leadership",
        resource_type="slack_private_channel",
        source_url=None,
        org_shared=False,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ValueError, match="organization-shared"):
            await CompanyBrainProviderClient(http_client).fetch_resource(
                "slack",
                OAuthToken(access_token="token"),
                resource,
                connection_id="connection-1",
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_google_discovery_proves_calendar_acl_and_lists_drive_children() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/drive/v3/drives"):
            return httpx.Response(200, json={"drives": [{"id": "drive-1", "name": "Shared"}]})
        if path.endswith("/drive/v3/files"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "folder-1",
                            "name": "Policies",
                            "mimeType": "application/vnd.google-apps.folder",
                            "webViewLink": "https://drive.google.com/drive/folders/folder-1",
                        },
                        {
                            "id": "doc-1",
                            "name": "Retention",
                            "mimeType": "application/vnd.google-apps.document",
                            "createdTime": "2026-08-01T10:00:00Z",
                            "modifiedTime": "2026-08-26T10:00:00Z",
                            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
                        },
                        {
                            "id": "pdf-1",
                            "name": "Security report.pdf",
                            "mimeType": "application/pdf",
                            "createdTime": "2026-08-01T10:00:00Z",
                            "modifiedTime": "2026-08-26T10:00:00Z",
                            "webViewLink": "https://drive.google.com/file/d/pdf-1/view",
                            "size": "1024",
                        },
                    ]
                },
            )
        if path.endswith("/users/me/calendarList"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "primary", "summary": "Personal", "primary": True},
                        {"id": "team@example.com", "summary": "Team calendar"},
                    ]
                },
            )
        if path.endswith("/calendars/team@example.com/acl"):
            return httpx.Response(
                200,
                json={"items": [{"scope": {"type": "domain"}, "role": "reader"}]},
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resources = await CompanyBrainProviderClient(http_client).discover_resources(
            "google", OAuthToken(access_token="token")
        )

    assert {(item.id, item.resource_type) for item in resources} == {
        ("drive-1", "google_shared_drive"),
        ("folder-1", "google_folder"),
        ("doc-1", "google_document"),
        ("pdf-1", "google_binary_file"),
        ("team@example.com", "google_shared_calendar"),
    }


@pytest.mark.asyncio
async def test_google_calendar_fetch_paginates_before_marking_complete() -> None:
    def event(event_id: str, updated: str) -> dict[str, object]:
        return {
            "id": event_id,
            "summary": f"Event {event_id}",
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
            "created": "2026-08-01T10:00:00Z",
            "updated": updated,
            "start": {"dateTime": "2026-08-27T10:00:00Z"},
            "end": {"dateTime": "2026-08-27T11:00:00Z"},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acl"):
            return httpx.Response(
                200,
                json={"items": [{"scope": {"type": "domain"}, "role": "reader"}]},
            )
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(200, json={"items": [event("2", "2026-08-26T11:00:00Z")]})
        return httpx.Response(
            200,
            json={
                "items": [event("1", "2026-08-26T10:00:00Z")],
                "nextPageToken": "next",
            },
        )

    resource = ProviderResource(
        id="team@example.com",
        name="Team calendar",
        resource_type="google_shared_calendar",
        source_url=None,
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await CompanyBrainProviderClient(http_client).fetch_resource(
            "google",
            OAuthToken(access_token="token"),
            resource,
            connection_id="connection-1",
        )

    assert result.complete is True
    assert [item.document.external_resource_id for item in result.documents] == [
        "team@example.com:1",
        "team@example.com:2",
    ]


@pytest.mark.asyncio
async def test_google_calendar_acl_discovery_is_bounded() -> None:
    acl_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal acl_calls
        path = request.url.path
        if path.endswith("/drive/v3/drives"):
            return httpx.Response(200, json={"drives": []})
        if path.endswith("/users/me/calendarList"):
            return httpx.Response(
                200,
                json={"items": [{"id": "large@example.com", "summary": "Large ACL"}]},
            )
        if path.endswith("/acl"):
            acl_calls += 1
            return httpx.Response(
                200,
                json={
                    "items": [{"scope": {"type": "user"}, "role": "reader"}],
                    "nextPageToken": f"page-{acl_calls}",
                },
            )
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resources = await CompanyBrainProviderClient(http_client).discover_resources(
            "google", OAuthToken(access_token="token")
        )

    assert resources == ()
    assert acl_calls == 20


@pytest.mark.asyncio
async def test_slack_thread_fetch_paginates_replies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("conversations.info"):
            return httpx.Response(200, json={"ok": True, "channel": {"is_archived": False}})
        if path.endswith("team.info"):
            return httpx.Response(200, json={"ok": True, "team": {"domain": "example"}})
        if path.endswith("users.list"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "members": [
                        {"id": "U1", "profile": {"display_name": "Ada"}},
                        {"id": "U2", "profile": {"display_name": "Ben"}},
                    ],
                },
            )
        if path.endswith("conversations.history"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1787752800.000100",
                            "user": "U1",
                            "text": "Decision",
                            "reply_count": 2,
                        }
                    ],
                },
            )
        if request.url.params.get("cursor") == "next":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "ts": "1787752920.000300",
                            "thread_ts": "1787752800.000100",
                            "user": "U2",
                            "text": "Second reply",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"ts": "1787752800.000100", "user": "U1", "text": "Decision"},
                    {
                        "ts": "1787752860.000200",
                        "thread_ts": "1787752800.000100",
                        "user": "U2",
                        "text": "First reply",
                    },
                ],
                "response_metadata": {"next_cursor": "next"},
            },
        )

    resource = ProviderResource(
        id="C1",
        name="#security",
        resource_type="slack_public_channel",
        source_url=None,
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await CompanyBrainProviderClient(http_client).fetch_resource(
            "slack",
            OAuthToken(access_token="token"),
            resource,
            connection_id="connection-1",
        )

    assert result.complete is True
    assert "First reply" in result.documents[0].document.markdown
    assert "Second reply" in result.documents[0].document.markdown


@pytest.mark.asyncio
async def test_fetch_rejects_crafted_private_slack_channel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"ok": True, "channel": {"is_private": True}},
        )

    resource = ProviderResource(
        id="G1",
        name="#leadership",
        resource_type="slack_public_channel",
        source_url=None,
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ValueError, match="not organization-shared"):
            await CompanyBrainProviderClient(http_client).fetch_resource(
                "slack",
                OAuthToken(access_token="token"),
                resource,
                connection_id="connection-1",
            )


@pytest.mark.asyncio
async def test_fetch_rejects_google_document_outside_shared_drive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "personal-doc",
                "name": "Personal",
                "mimeType": "application/vnd.google-apps.document",
                "createdTime": "2026-08-01T10:00:00Z",
                "modifiedTime": "2026-08-26T10:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/personal-doc/edit",
                "trashed": False,
            },
        )

    resource = ProviderResource(
        id="personal-doc",
        name="Personal",
        resource_type="google_document",
        source_url="https://docs.google.com/document/d/personal-doc/edit",
        org_shared=True,
        metadata={},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ValueError, match="not in a Shared Drive"):
            await CompanyBrainProviderClient(http_client).fetch_resource(
                "google",
                OAuthToken(access_token="token"),
                resource,
                connection_id="connection-1",
            )
