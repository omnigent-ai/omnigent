from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping
from typing import Any

from omnigent_company_brain.adapters.common import (
    AdaptedDocument,
    OrgSharedRequiredError,
    build_binary_document,
    build_document,
    markdown_frontmatter,
    parse_timestamp,
)
from omnigent_company_brain.models import normalize_markdown

GOOGLE_BINARY_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
GOOGLE_BINARY_MAX_BYTES = 25 * 1024 * 1024
GOOGLE_OPENXML_MAX_MEMBERS = 10_000
GOOGLE_OPENXML_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
GOOGLE_SHEET_MAX_COLUMNS = 50
GOOGLE_SHEET_MAX_ROWS = 500


def _google_markdown(
    *,
    title: str,
    external_id: str,
    source_url: str,
    modified_at: Any,
    body: str,
    link_label: str,
) -> str:
    frontmatter = markdown_frontmatter(
        title=title,
        provider="google",
        external_resource_id=external_id,
        source_url=source_url,
        modified_at=modified_at,
    )
    return (
        f"{frontmatter}\n\n# {title}\n\n{normalize_markdown(body)}\n[{link_label}]({source_url})\n"
    )


def transform_google_document(
    *,
    connection_id: str,
    file: Mapping[str, Any],
    exported_markdown: str,
    org_shared: bool,
) -> AdaptedDocument:
    if not org_shared:
        raise OrgSharedRequiredError("Google resource is not organization-shared")
    external_id = str(file["id"])
    title = str(file["name"]).strip()
    source_url = str(file["webViewLink"])
    modified_at = parse_timestamp(str(file["modifiedTime"]))
    if modified_at is None:
        raise ValueError("Google modifiedTime is required")
    markdown = _google_markdown(
        title=title,
        external_id=external_id,
        source_url=source_url,
        modified_at=modified_at,
        body=exported_markdown,
        link_label="Open in Google Workspace",
    )
    return build_document(
        provider="google",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=markdown,
        source_url=source_url,
        created_at=file.get("createdTime"),
        modified_at=modified_at,
        raw_payload={"file": dict(file), "exported_markdown": exported_markdown},
        transform_schema_version="google-doc.v1",
    )


def transform_google_sheet(
    *,
    connection_id: str,
    file: Mapping[str, Any],
    exported_csv: str,
    org_shared: bool,
) -> AdaptedDocument:
    if not org_shared:
        raise OrgSharedRequiredError("Google resource is not organization-shared")
    rows: list[list[str]] = []
    rows_truncated = False
    columns_truncated = False
    for index, row in enumerate(csv.reader(io.StringIO(exported_csv))):
        if index >= GOOGLE_SHEET_MAX_ROWS:
            rows_truncated = True
            break
        if len(row) > GOOGLE_SHEET_MAX_COLUMNS:
            columns_truncated = True
        rows.append(row[:GOOGLE_SHEET_MAX_COLUMNS])
    width = max((len(row) for row in rows), default=0)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", "<br>")

    if padded:
        body_lines = ["| " + " | ".join(cell(value) for value in padded[0]) + " |"]
        body_lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        body_lines.extend(
            "| " + " | ".join(cell(value) for value in row) + " |" for row in padded[1:]
        )
        if rows_truncated or columns_truncated:
            body_lines.extend(
                [
                    "",
                    "_Table limited to 500 rows and 50 columns; "
                    "the full CSV is preserved as provenance._",
                ]
            )
        body = "\n".join(body_lines)
    else:
        body = "_Empty sheet_"
    external_id = str(file["id"])
    title = str(file["name"]).strip()
    source_url = str(file["webViewLink"])
    modified_at = parse_timestamp(str(file["modifiedTime"]))
    if modified_at is None:
        raise ValueError("Google modifiedTime is required")
    return build_binary_document(
        provider="google",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=_google_markdown(
            title=title,
            external_id=external_id,
            source_url=source_url,
            modified_at=modified_at,
            body=body,
            link_label="Open in Google Sheets",
        ),
        source_url=source_url,
        created_at=file.get("createdTime"),
        modified_at=modified_at,
        raw_bytes=exported_csv.encode("utf-8"),
        raw_metadata={"file": dict(file)},
        media_type="text/csv; charset=utf-8",
        extension="csv",
        transform_schema_version="google-sheet.v1",
    )


def transform_google_slides(
    *,
    connection_id: str,
    file: Mapping[str, Any],
    exported_text: str,
    org_shared: bool,
) -> AdaptedDocument:
    if not org_shared:
        raise OrgSharedRequiredError("Google resource is not organization-shared")
    slides = [part.strip() for part in exported_text.replace("\r\n", "\n").split("\f")]
    body = "\n\n".join(
        f"## Slide {index}\n\n{content or '_No text_'}"
        for index, content in enumerate(slides, start=1)
    )
    external_id = str(file["id"])
    title = str(file["name"]).strip()
    source_url = str(file["webViewLink"])
    modified_at = parse_timestamp(str(file["modifiedTime"]))
    if modified_at is None:
        raise ValueError("Google modifiedTime is required")
    return build_document(
        provider="google",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=_google_markdown(
            title=title,
            external_id=external_id,
            source_url=source_url,
            modified_at=modified_at,
            body=body,
            link_label="Open in Google Slides",
        ),
        source_url=source_url,
        created_at=file.get("createdTime"),
        modified_at=modified_at,
        raw_payload={"file": dict(file), "exported_text": exported_text},
        transform_schema_version="google-slides.v1",
    )


def transform_google_binary_file(
    *,
    connection_id: str,
    file: Mapping[str, Any],
    content: bytes,
    org_shared: bool,
) -> AdaptedDocument:
    from markitdown import MarkItDown

    if not org_shared:
        raise OrgSharedRequiredError("Google resource is not organization-shared")
    media_type = str(file["mimeType"])
    extension = GOOGLE_BINARY_EXTENSIONS.get(media_type)
    if extension is None:
        raise ValueError("Unsupported Google binary document type")
    if len(content) > GOOGLE_BINARY_MAX_BYTES:
        raise ValueError("Google binary document exceeds the 25 MiB conversion limit")
    if extension in {"docx", "pptx", "xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
        except zipfile.BadZipFile as exc:
            raise ValueError("Google OpenXML document is not a valid archive") from exc
        if (
            len(members) > GOOGLE_OPENXML_MAX_MEMBERS
            or sum(member.file_size for member in members) > GOOGLE_OPENXML_MAX_UNCOMPRESSED_BYTES
        ):
            raise ValueError("Google OpenXML document exceeds the expanded-content limit")
    converted = MarkItDown(enable_plugins=False).convert_stream(
        io.BytesIO(content),
        file_extension=f".{extension}",
    )
    body = normalize_markdown(converted.text_content)
    if not body:
        raise ValueError("Google binary document produced no extractable text")
    external_id = str(file["id"])
    title = str(file["name"]).strip()
    source_url = str(file["webViewLink"])
    modified_at = parse_timestamp(str(file["modifiedTime"]))
    if modified_at is None:
        raise ValueError("Google modifiedTime is required")
    return build_binary_document(
        provider="google",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=_google_markdown(
            title=title,
            external_id=external_id,
            source_url=source_url,
            modified_at=modified_at,
            body=body,
            link_label="Open original in Google Drive",
        ),
        source_url=source_url,
        created_at=file.get("createdTime"),
        modified_at=modified_at,
        raw_bytes=content,
        raw_metadata={"file": dict(file)},
        media_type=media_type,
        extension=extension,
        transform_schema_version="google-binary-markitdown.v1",
    )


def transform_google_calendar_event(
    *,
    connection_id: str,
    calendar_id: str,
    calendar_name: str,
    event: Mapping[str, Any],
    org_shared: bool,
) -> AdaptedDocument:
    if not org_shared:
        raise OrgSharedRequiredError("Google calendar is not organization-shared")
    external_id = f"{calendar_id}:{event['id']}"
    title = str(event.get("summary") or "Untitled calendar event").strip()
    source_url = str(event.get("htmlLink") or "")
    modified_at = parse_timestamp(str(event["updated"]))
    if modified_at is None:
        raise ValueError("Google calendar event updated timestamp is required")
    start = event.get("start") or {}
    end = event.get("end") or {}
    attendees = sorted(
        str(attendee.get("displayName") or attendee.get("email") or "")
        for attendee in event.get("attendees") or []
        if attendee.get("displayName") or attendee.get("email")
    )
    frontmatter = markdown_frontmatter(
        title=title,
        provider="google",
        external_resource_id=external_id,
        source_url=source_url,
        modified_at=modified_at,
    )
    details = [
        f"Calendar: {calendar_name}",
        f"Start: {start.get('dateTime') or start.get('date') or 'Unknown'}",
        f"End: {end.get('dateTime') or end.get('date') or 'Unknown'}",
    ]
    if event.get("location"):
        details.append(f"Location: {event['location']}")
    if attendees:
        details.append(f"Attendees: {', '.join(attendees)}")
    description = str(event.get("description") or "").strip()
    rendered_details = "\n".join(f"{detail}  " for detail in details)
    rendered_description = f"\n\n{description}" if description else ""
    markdown = (
        f"{frontmatter}\n\n# {title}\n\n{rendered_details}"
        f"{rendered_description}\n\n[Open in Google Calendar]({source_url})\n"
    )
    return build_document(
        provider="google",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=markdown,
        source_url=source_url,
        created_at=event.get("created"),
        modified_at=modified_at,
        raw_payload={"calendar_id": calendar_id, "event": dict(event)},
        transform_schema_version="google-calendar-event.v1",
    )
