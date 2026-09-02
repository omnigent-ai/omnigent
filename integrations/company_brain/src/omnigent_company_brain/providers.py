from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import httpx

from omnigent_company_brain.adapters import (
    GOOGLE_BINARY_EXTENSIONS,
    GOOGLE_BINARY_MAX_BYTES,
    AdaptedDocument,
    transform_google_binary_file,
    transform_google_calendar_event,
    transform_google_document,
    transform_google_sheet,
    transform_google_slides,
    transform_notion_page,
    transform_slack_thread,
)
from omnigent_company_brain.oauth import OAuthToken, ProviderName

Sleep = Callable[[float], Awaitable[None]]

_GOOGLE_RESOURCE_TYPES = {
    "application/vnd.google-apps.folder": "google_folder",
    "application/vnd.google-apps.document": "google_document",
    "application/vnd.google-apps.spreadsheet": "google_sheet",
    "application/vnd.google-apps.presentation": "google_slides",
    **dict.fromkeys(GOOGLE_BINARY_EXTENSIONS, "google_binary_file"),
}
_GOOGLE_NATIVE_MIME_TYPES = {
    "google_document": "application/vnd.google-apps.document",
    "google_folder": "application/vnd.google-apps.folder",
    "google_sheet": "application/vnd.google-apps.spreadsheet",
    "google_slides": "application/vnd.google-apps.presentation",
}
_GOOGLE_MAX_ACL_PAGES = 20


@dataclass(frozen=True, slots=True)
class ProviderResource:
    id: str
    name: str
    resource_type: str
    source_url: str | None
    org_shared: bool
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProviderFetchResult:
    documents: tuple[AdaptedDocument, ...]
    complete: bool


class ProviderRequestError(RuntimeError):
    pass


class CompanyBrainProviderClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = 4,
    ) -> None:
        self._http = http_client
        self._sleep = sleep
        self._max_attempts = max_attempts

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_attempts):
            response = await self._http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == self._max_attempts:
                    break
                retry_after = response.headers.get("retry-after")
                delay = min(float(retry_after), 30.0) if retry_after else min(2**attempt, 8)
                await self._sleep(delay)
                continue
            if not response.is_success:
                raise ProviderRequestError(f"provider request failed ({response.status_code})")
            return response
        raise ProviderRequestError("provider request exhausted retry budget")

    @staticmethod
    def _bearer(token: OAuthToken) -> dict[str, str]:
        return {"authorization": f"Bearer {token.access_token}"}

    async def discover_resources(
        self,
        provider: ProviderName,
        token: OAuthToken,
    ) -> tuple[ProviderResource, ...]:
        if provider == "google":
            return await self._discover_google(token)
        if provider == "slack":
            return await self._discover_slack(token)
        return await self._discover_notion(token)

    async def fetch_resource(
        self,
        provider: ProviderName,
        token: OAuthToken,
        resource: ProviderResource,
        *,
        connection_id: str,
        limit: int | None = None,
    ) -> ProviderFetchResult:
        if not resource.org_shared:
            raise ValueError("resource is outside the organization-shared visibility class")
        if provider == "google":
            return await self._fetch_google(
                token,
                resource,
                connection_id=connection_id,
                limit=limit,
            )
        if provider == "slack":
            return await self._fetch_slack(
                token,
                resource,
                connection_id=connection_id,
                limit=limit,
            )
        return await self._fetch_notion(
            token,
            resource,
            connection_id=connection_id,
        )

    async def _discover_google(self, token: OAuthToken) -> tuple[ProviderResource, ...]:
        headers = self._bearer(token)
        resources: list[ProviderResource] = []
        page_token: str | None = None
        while True:
            response = await self._request(
                "GET",
                "https://www.googleapis.com/drive/v3/drives",
                headers=headers,
                params={
                    "pageSize": 100,
                    "fields": "nextPageToken,drives(id,name)",
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            payload = response.json()
            for drive in payload.get("drives") or []:
                resources.append(
                    ProviderResource(
                        id=str(drive["id"]),
                        name=str(drive["name"]),
                        resource_type="google_shared_drive",
                        source_url=f"https://drive.google.com/drive/folders/{drive['id']}",
                        org_shared=True,
                        metadata={"drive_id": str(drive["id"])},
                    )
                )
                resources.extend(await self._discover_google_drive_items(headers, drive))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        page_token = None
        while True:
            response = await self._request(
                "GET",
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers=headers,
                params={
                    "maxResults": 250,
                    "showDeleted": "false",
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            payload = response.json()
            for calendar in payload.get("items") or []:
                if calendar.get("primary"):
                    continue
                calendar_id = str(calendar["id"])
                if not await self._google_calendar_is_org_shared(headers, calendar_id):
                    continue
                resources.append(
                    ProviderResource(
                        id=calendar_id,
                        name=str(calendar.get("summary") or calendar_id),
                        resource_type="google_shared_calendar",
                        source_url=None,
                        org_shared=True,
                        metadata={"time_zone": str(calendar.get("timeZone") or "UTC")},
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return tuple(resources)

    async def _discover_google_drive_items(
        self,
        headers: Mapping[str, str],
        drive: Mapping[str, Any],
    ) -> list[ProviderResource]:
        resources: list[ProviderResource] = []
        page_token: str | None = None
        while True:
            response = await self._request(
                "GET",
                "https://www.googleapis.com/drive/v3/files",
                headers=headers,
                params={
                    "corpora": "drive",
                    "driveId": drive["id"],
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                    "q": "trashed = false",
                    "pageSize": 1000,
                    "fields": (
                        "nextPageToken,files("
                        "id,name,mimeType,createdTime,modifiedTime,webViewLink,parents,size)"
                    ),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            payload = response.json()
            for file in payload.get("files") or []:
                mime_type = str(file["mimeType"])
                resource_type = _GOOGLE_RESOURCE_TYPES.get(mime_type)
                if resource_type is None:
                    continue
                resources.append(
                    ProviderResource(
                        id=str(file["id"]),
                        name=str(file["name"]),
                        resource_type=resource_type,
                        source_url=str(file.get("webViewLink") or "") or None,
                        org_shared=True,
                        metadata={
                            "drive_id": str(drive["id"]),
                            "mime_type": mime_type,
                            "created_time": str(file.get("createdTime") or ""),
                            "modified_time": str(file.get("modifiedTime") or ""),
                            "web_view_link": str(file.get("webViewLink") or ""),
                        },
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return resources

    async def _google_calendar_is_org_shared(
        self,
        headers: Mapping[str, str],
        calendar_id: str,
    ) -> bool:
        page_token: str | None = None
        for _ in range(_GOOGLE_MAX_ACL_PAGES):
            response = await self._request(
                "GET",
                (
                    "https://www.googleapis.com/calendar/v3/calendars/"
                    f"{quote(calendar_id, safe='')}/acl"
                ),
                headers=headers,
                params={
                    "maxResults": 250,
                    "showDeleted": "false",
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            payload = response.json()
            if any(
                (item.get("scope") or {}).get("type") in {"domain", "default"}
                and item.get("role") in {"freeBusyReader", "reader", "writer", "owner"}
                for item in payload.get("items") or []
            ):
                return True
            page_token = payload.get("nextPageToken")
            if not page_token:
                return False
        return False

    async def _discover_slack(self, token: OAuthToken) -> tuple[ProviderResource, ...]:
        headers = self._bearer(token)
        resources: list[ProviderResource] = []
        cursor = ""
        while True:
            response = await self._request(
                "GET",
                "https://slack.com/api/conversations.list",
                headers=headers,
                params={
                    "types": "public_channel",
                    "exclude_archived": "true",
                    "limit": 200,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            payload = response.json()
            if payload.get("ok") is not True:
                raise ProviderRequestError("Slack rejected channel discovery")
            resources.extend(
                ProviderResource(
                    id=str(channel["id"]),
                    name=f"#{channel['name']}",
                    resource_type="slack_public_channel",
                    source_url=None,
                    org_shared=not bool(channel.get("is_private")),
                    metadata={},
                )
                for channel in payload.get("channels") or []
                if not channel.get("is_private")
            )
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return tuple(resources)

    async def _discover_notion(self, token: OAuthToken) -> tuple[ProviderResource, ...]:
        headers = {
            **self._bearer(token),
            "notion-version": "2022-06-28",
            "content-type": "application/json",
        }
        resources: list[ProviderResource] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            response = await self._request(
                "POST",
                "https://api.notion.com/v1/search",
                headers=headers,
                json_body=body,
            )
            payload = response.json()
            for page in payload.get("results") or []:
                if page.get("archived") or page.get("in_trash"):
                    continue
                resources.append(
                    ProviderResource(
                        id=str(page["id"]),
                        name=_notion_title(page),
                        resource_type="notion_page",
                        source_url=str(page.get("url") or ""),
                        org_shared=True,
                        metadata={},
                    )
                )
            cursor = payload.get("next_cursor") if payload.get("has_more") else None
            if not cursor:
                break
        return tuple(resources)

    async def _fetch_google(
        self,
        token: OAuthToken,
        resource: ProviderResource,
        *,
        connection_id: str,
        limit: int | None,
    ) -> ProviderFetchResult:
        headers = self._bearer(token)
        if resource.resource_type == "google_shared_calendar":
            if not await self._google_calendar_is_org_shared(headers, resource.id):
                raise ValueError("Google calendar is not organization-shared")
            events: list[dict[str, Any]] = []
            page_token: str | None = None
            while True:
                response = await self._request(
                    "GET",
                    (
                        "https://www.googleapis.com/calendar/v3/calendars/"
                        f"{quote(resource.id, safe='')}/events"
                    ),
                    headers=headers,
                    params={
                        "singleEvents": "true",
                        "showDeleted": "false",
                        "maxResults": min(limit or 2500, 2500),
                        **({"pageToken": page_token} if page_token else {}),
                    },
                )
                payload = response.json()
                events.extend(payload.get("items") or [])
                if limit is not None and len(events) >= limit:
                    break
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
            calendar_documents = tuple(
                transform_google_calendar_event(
                    connection_id=connection_id,
                    calendar_id=resource.id,
                    calendar_name=resource.name,
                    event=event,
                    org_shared=True,
                )
                for event in events[:limit]
            )
            return ProviderFetchResult(documents=calendar_documents, complete=limit is None)

        if resource.resource_type == "google_shared_drive":
            await self._request(
                "GET",
                f"https://www.googleapis.com/drive/v3/drives/{resource.id}",
                headers=headers,
                params={"fields": "id,name"},
            )
            files = await self._google_documents_for_container(
                headers,
                resource,
                limit=limit,
            )
        elif resource.resource_type in {
            "google_document",
            "google_folder",
            "google_sheet",
            "google_slides",
            "google_binary_file",
        }:
            response = await self._request(
                "GET",
                f"https://www.googleapis.com/drive/v3/files/{resource.id}",
                headers=headers,
                params={
                    "supportsAllDrives": "true",
                    "fields": (
                        "id,name,mimeType,createdTime,modifiedTime,webViewLink,"
                        "trashed,driveId,size"
                    ),
                },
            )
            metadata = response.json()
            if metadata.get("trashed"):
                return ProviderFetchResult(documents=(), complete=True)
            if not metadata.get("driveId"):
                raise ValueError("Google resource is not in a Shared Drive")
            mime_type = str(metadata.get("mimeType") or "")
            expected_mime_type = _GOOGLE_NATIVE_MIME_TYPES.get(resource.resource_type)
            if (expected_mime_type is not None and mime_type != expected_mime_type) or (
                resource.resource_type == "google_binary_file"
                and mime_type not in GOOGLE_BINARY_EXTENSIONS
            ):
                raise ValueError("Selected Google resource type does not match provider metadata")
            if resource.resource_type != "google_folder":
                files = [metadata]
            else:
                verified_resource = replace(
                    resource,
                    metadata={**resource.metadata, "drive_id": str(metadata["driveId"])},
                )
                files = await self._google_documents_for_container(
                    headers,
                    verified_resource,
                    limit=limit,
                )
        else:
            raise ValueError("Unsupported Google resource type")
        documents: list[AdaptedDocument] = []
        for file in files[:limit]:
            mime_type = str(file["mimeType"])
            if mime_type in GOOGLE_BINARY_EXTENSIONS:
                declared_size = int(file.get("size") or 0)
                if declared_size > GOOGLE_BINARY_MAX_BYTES:
                    raise ValueError("Google binary document exceeds the 25 MiB conversion limit")
                downloaded = await self._request(
                    "GET",
                    f"https://www.googleapis.com/drive/v3/files/{file['id']}",
                    headers=headers,
                    params={"alt": "media", "supportsAllDrives": "true"},
                )
                documents.append(
                    await asyncio.to_thread(
                        transform_google_binary_file,
                        connection_id=connection_id,
                        file=file,
                        content=downloaded.content,
                        org_shared=True,
                    )
                )
                continue
            export_type = {
                "application/vnd.google-apps.document": "text/markdown",
                "application/vnd.google-apps.spreadsheet": "text/csv",
                "application/vnd.google-apps.presentation": "text/plain",
            }.get(mime_type)
            if export_type is None:
                continue
            exported = await self._request(
                "GET",
                f"https://www.googleapis.com/drive/v3/files/{file['id']}/export",
                headers=headers,
                params={"mimeType": export_type},
            )
            transform = {
                "application/vnd.google-apps.document": transform_google_document,
                "application/vnd.google-apps.spreadsheet": transform_google_sheet,
                "application/vnd.google-apps.presentation": transform_google_slides,
            }[mime_type]
            payload_name = {
                "application/vnd.google-apps.document": "exported_markdown",
                "application/vnd.google-apps.spreadsheet": "exported_csv",
                "application/vnd.google-apps.presentation": "exported_text",
            }[mime_type]
            documents.append(
                transform(
                    connection_id=connection_id,
                    file=file,
                    org_shared=True,
                    **{payload_name: exported.text},
                )
            )
        return ProviderFetchResult(documents=tuple(documents), complete=limit is None)

    async def _google_documents_for_container(
        self,
        headers: Mapping[str, str],
        resource: ProviderResource,
        *,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        pending_folders = [resource.id] if resource.resource_type == "google_folder" else []
        listed_folders: set[str] = set()
        drive_id = resource.metadata.get("drive_id") or resource.id
        while resource.resource_type == "google_shared_drive" or pending_folders:
            folder_id = pending_folders.pop(0) if pending_folders else None
            if folder_id and folder_id in listed_folders:
                continue
            if folder_id:
                listed_folders.add(folder_id)
            page_token: str | None = None
            while True:
                query = "trashed = false"
                if folder_id:
                    query += f" and '{folder_id}' in parents"
                else:
                    query += " and mimeType != 'application/vnd.google-apps.folder'"
                response = await self._request(
                    "GET",
                    "https://www.googleapis.com/drive/v3/files",
                    headers=headers,
                    params={
                        "corpora": "drive",
                        "driveId": drive_id,
                        "includeItemsFromAllDrives": "true",
                        "supportsAllDrives": "true",
                        "q": query,
                        "pageSize": min(limit or 1000, 1000),
                        "fields": (
                            "nextPageToken,files("
                            "id,name,mimeType,createdTime,modifiedTime,webViewLink,parents,size)"
                        ),
                        **({"pageToken": page_token} if page_token else {}),
                    },
                )
                payload = response.json()
                for file in payload.get("files") or []:
                    if file.get("mimeType") == "application/vnd.google-apps.folder":
                        pending_folders.append(str(file["id"]))
                    elif file.get("mimeType") in {
                        "application/vnd.google-apps.document",
                        "application/vnd.google-apps.spreadsheet",
                        "application/vnd.google-apps.presentation",
                        *GOOGLE_BINARY_EXTENSIONS,
                    }:
                        files.append(file)
                        if limit is not None and len(files) >= limit:
                            return files
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
            if resource.resource_type == "google_shared_drive":
                break
        return files

    async def _fetch_slack(
        self,
        token: OAuthToken,
        resource: ProviderResource,
        *,
        connection_id: str,
        limit: int | None,
    ) -> ProviderFetchResult:
        headers = self._bearer(token)
        channel_info = await self._request(
            "GET",
            "https://slack.com/api/conversations.info",
            headers=headers,
            params={"channel": resource.id},
        )
        channel_payload = channel_info.json()
        if channel_payload.get("ok") is not True:
            raise ProviderRequestError("Slack rejected channel lookup")
        channel = channel_payload.get("channel") or {}
        if channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim"):
            raise ValueError("Slack channel is not organization-shared")
        if channel.get("is_archived"):
            return ProviderFetchResult(documents=(), complete=True)
        team_response = await self._request(
            "GET", "https://slack.com/api/team.info", headers=headers
        )
        team_payload = team_response.json()
        if team_payload.get("ok") is not True:
            raise ProviderRequestError("Slack rejected team lookup")
        domain = str((team_payload.get("team") or {}).get("domain") or "workspace")
        users = await self._slack_users(headers)
        messages: list[dict[str, Any]] = []
        cursor = ""
        while True:
            response = await self._request(
                "GET",
                "https://slack.com/api/conversations.history",
                headers=headers,
                params={
                    "channel": resource.id,
                    "limit": 200,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            payload = response.json()
            if payload.get("ok") is not True:
                raise ProviderRequestError("Slack rejected channel history fetch")
            messages.extend(payload.get("messages") or [])
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        roots = [message for message in messages if not message.get("thread_ts")]
        documents: list[AdaptedDocument] = []
        for root in roots[:limit]:
            thread = [root]
            if int(root.get("reply_count") or 0) > 0:
                thread_by_timestamp: dict[str, dict[str, Any]] = {}
                reply_cursor = ""
                while True:
                    replies = await self._request(
                        "GET",
                        "https://slack.com/api/conversations.replies",
                        headers=headers,
                        params={
                            "channel": resource.id,
                            "ts": root["ts"],
                            "limit": 200,
                            **({"cursor": reply_cursor} if reply_cursor else {}),
                        },
                    )
                    reply_payload = replies.json()
                    if reply_payload.get("ok") is not True:
                        raise ProviderRequestError("Slack rejected thread fetch")
                    for message in reply_payload.get("messages") or []:
                        thread_by_timestamp[str(message["ts"])] = message
                    reply_cursor = str(
                        (reply_payload.get("response_metadata") or {}).get("next_cursor") or ""
                    )
                    if not reply_cursor:
                        break
                thread = list(thread_by_timestamp.values()) or thread
            documents.append(
                transform_slack_thread(
                    connection_id=connection_id,
                    workspace_domain=domain,
                    channel={"id": resource.id, "name": resource.name.removeprefix("#")},
                    messages=thread,
                    users=users,
                )
            )
        return ProviderFetchResult(documents=tuple(documents), complete=limit is None)

    async def _slack_users(self, headers: Mapping[str, str]) -> dict[str, str]:
        users: dict[str, str] = {}
        cursor = ""
        while True:
            response = await self._request(
                "GET",
                "https://slack.com/api/users.list",
                headers=headers,
                params={"limit": 200, **({"cursor": cursor} if cursor else {})},
            )
            payload = response.json()
            if payload.get("ok") is not True:
                raise ProviderRequestError("Slack rejected user lookup")
            for member in payload.get("members") or []:
                profile = member.get("profile") or {}
                users[str(member["id"])] = str(
                    profile.get("display_name") or profile.get("real_name") or member["id"]
                )
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return users

    async def _fetch_notion(
        self,
        token: OAuthToken,
        resource: ProviderResource,
        *,
        connection_id: str,
    ) -> ProviderFetchResult:
        headers = {
            **self._bearer(token),
            "notion-version": "2022-06-28",
        }
        page = (
            await self._request(
                "GET",
                f"https://api.notion.com/v1/pages/{resource.id}",
                headers=headers,
            )
        ).json()
        if page.get("archived") or page.get("in_trash"):
            return ProviderFetchResult(documents=(), complete=True)
        blocks = await self._notion_blocks(resource.id, headers)
        document = transform_notion_page(
            connection_id=connection_id,
            page=page,
            blocks=blocks,
            org_shared=True,
        )
        return ProviderFetchResult(documents=(document,), complete=True)

    async def _notion_blocks(
        self,
        block_id: str,
        headers: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await self._request(
                "GET",
                f"https://api.notion.com/v1/blocks/{block_id}/children",
                headers=headers,
                params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})},
            )
            payload = response.json()
            for block in payload.get("results") or []:
                if block.get("has_children"):
                    block = {
                        **block,
                        "children": await self._notion_blocks(str(block["id"]), headers),
                    }
                blocks.append(block)
            cursor = payload.get("next_cursor") if payload.get("has_more") else None
            if not cursor:
                break
        return blocks


def _notion_title(page: Mapping[str, Any]) -> str:
    for value in (page.get("properties") or {}).values():
        if value.get("type") != "title":
            continue
        title = "".join(str(item.get("plain_text") or "") for item in value.get("title") or [])
        if title.strip():
            return title.strip()
    return "Untitled Notion page"
