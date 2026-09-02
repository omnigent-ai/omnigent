from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BrainProvider = Literal["google", "slack", "notion"]
DeletionState = Literal["active", "deleted"]
VisibilityClass = Literal["org-shared"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_markdown(value: str) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(line.rstrip() for line in lines).strip()
    return f"{normalized}\n" if normalized else ""


def stable_document_path(
    provider: BrainProvider,
    connection_id: str,
    external_resource_id: str,
) -> str:
    slug = _SLUG_PATTERN.sub("-", external_resource_id.lower()).strip("-")[:48]
    digest = sha256_text(f"{provider}\0{connection_id}\0{external_resource_id}")[:12]
    return f"sources/{provider}/{slug or 'document'}-{digest}.md"


class BrainDocumentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["brain-document.v1"] = "brain-document.v1"
    provider: BrainProvider
    connection_id: str = Field(min_length=1, max_length=128)
    external_resource_id: str = Field(min_length=1, max_length=512)
    stable_path: str = Field(min_length=1, max_length=1024)
    title: str = Field(min_length=1, max_length=512)
    markdown: str
    canonical_source_url: str = Field(min_length=1, max_length=2048)
    source_created_at: datetime | None = None
    source_modified_at: datetime
    content_sha256: str
    raw_object_reference: str = Field(min_length=1, max_length=2048)
    raw_sha256: str
    transform_schema_version: str = Field(min_length=1, max_length=64)
    deletion_state: DeletionState = "active"
    visibility_class: VisibilityClass = "org-shared"

    @field_validator("source_created_at", "source_modified_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("source timestamps must include a timezone")
        return value

    @field_validator("canonical_source_url")
    @classmethod
    def _require_https_source(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("canonical_source_url must be an absolute HTTPS URL")
        return value

    @field_validator("content_sha256", "raw_sha256")
    @classmethod
    def _require_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("hashes must be lowercase SHA-256 hex values")
        return value

    @model_validator(mode="after")
    def _validate_publication_contract(self) -> Self:
        path = PurePosixPath(self.stable_path)
        raw_path = PurePosixPath(self.raw_object_reference)
        expected_prefix = ("sources", self.provider)
        if (
            path.is_absolute()
            or "\\" in self.stable_path
            or ".." in path.parts
            or path.parts[:2] != expected_prefix
            or path.suffix != ".md"
        ):
            raise ValueError(f"stable_path must be a safe sources/{self.provider}/... .md path")
        if (
            raw_path.is_absolute()
            or "\\" in self.raw_object_reference
            or ".." in raw_path.parts
            or raw_path.parts[:2] != (".raw", self.provider)
            or raw_path.suffix != ".json"
        ):
            raise ValueError(
                f"raw_object_reference must be a safe .raw/{self.provider}/... .json path"
            )
        if sha256_text(self.markdown) != self.content_sha256:
            raise ValueError("content_sha256 does not match markdown")
        if self.deletion_state == "active" and self.canonical_source_url not in self.markdown:
            raise ValueError("active markdown must contain canonical_source_url")
        return self
