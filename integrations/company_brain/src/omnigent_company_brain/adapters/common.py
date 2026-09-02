from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from omnigent_company_brain.models import (
    BrainDocumentV1,
    BrainProvider,
    normalize_markdown,
    sha256_bytes,
    sha256_text,
    stable_document_path,
)


class OrgSharedRequiredError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdaptedDocument:
    document: BrainDocumentV1
    raw_json: str
    raw_bytes: bytes | None = None
    raw_object_key: str | None = None


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("source timestamps must include a timezone")
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def markdown_frontmatter(
    *,
    title: str,
    provider: BrainProvider,
    external_resource_id: str,
    source_url: str,
    modified_at: datetime,
) -> str:
    fields = (
        ("title", title),
        ("type", "source"),
        ("visibility", "world"),
        ("company_brain_visibility", "org-shared"),
        ("provider", provider),
        ("external_resource_id", external_resource_id),
        ("source_url", source_url),
        ("source_modified_at", modified_at.isoformat().replace("+00:00", "Z")),
    )
    lines = [
        "---",
        *(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields),
        "---",
    ]
    return "\n".join(lines)


def build_document(
    *,
    provider: BrainProvider,
    connection_id: str,
    external_resource_id: str,
    title: str,
    markdown: str,
    source_url: str,
    created_at: str | datetime | None,
    modified_at: str | datetime,
    raw_payload: Any,
    transform_schema_version: str,
) -> AdaptedDocument:
    normalized = normalize_markdown(markdown)
    raw_json = canonical_json(raw_payload)
    raw_sha256 = sha256_text(raw_json)
    parsed_modified_at = parse_timestamp(modified_at)
    if parsed_modified_at is None:
        raise ValueError("modified_at is required")
    document = BrainDocumentV1(
        provider=provider,
        connection_id=connection_id,
        external_resource_id=external_resource_id,
        stable_path=stable_document_path(provider, connection_id, external_resource_id),
        title=title,
        markdown=normalized,
        canonical_source_url=source_url,
        source_created_at=parse_timestamp(created_at),
        source_modified_at=parsed_modified_at,
        content_sha256=sha256_text(normalized),
        raw_object_reference=f".raw/{provider}/{raw_sha256}.json",
        raw_sha256=raw_sha256,
        transform_schema_version=transform_schema_version,
    )
    return AdaptedDocument(document=document, raw_json=raw_json)


def build_binary_document(
    *,
    provider: BrainProvider,
    connection_id: str,
    external_resource_id: str,
    title: str,
    markdown: str,
    source_url: str,
    created_at: str | datetime | None,
    modified_at: str | datetime,
    raw_bytes: bytes,
    raw_metadata: Any,
    media_type: str,
    extension: str,
    transform_schema_version: str,
) -> AdaptedDocument:
    normalized = normalize_markdown(markdown)
    raw_sha256 = sha256_bytes(raw_bytes)
    normalized_extension = extension.lower().lstrip(".")
    if not normalized_extension.isalnum():
        raise ValueError("binary extension must be alphanumeric")
    raw_object_key = f"company-brain/raw/{provider}/{raw_sha256}.{normalized_extension}"
    raw_json = canonical_json(
        {
            "artifact_key": raw_object_key,
            "content_type": media_type,
            "metadata": raw_metadata,
            "sha256": raw_sha256,
            "size_bytes": len(raw_bytes),
        }
    )
    parsed_modified_at = parse_timestamp(modified_at)
    if parsed_modified_at is None:
        raise ValueError("modified_at is required")
    document = BrainDocumentV1(
        provider=provider,
        connection_id=connection_id,
        external_resource_id=external_resource_id,
        stable_path=stable_document_path(provider, connection_id, external_resource_id),
        title=title,
        markdown=normalized,
        canonical_source_url=source_url,
        source_created_at=parse_timestamp(created_at),
        source_modified_at=parsed_modified_at,
        content_sha256=sha256_text(normalized),
        raw_object_reference=f".raw/{provider}/{raw_sha256}.json",
        raw_sha256=raw_sha256,
        transform_schema_version=transform_schema_version,
    )
    return AdaptedDocument(
        document=document,
        raw_json=raw_json,
        raw_bytes=raw_bytes,
        raw_object_key=raw_object_key,
    )
