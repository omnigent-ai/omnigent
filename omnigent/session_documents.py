"""Google Docs URL validation and shared session references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GOOGLE_DOCS_ORIGIN = "https://docs.google.com"
_EDITOR_PATH = re.compile(
    r"/document/(?:u/[0-9]+/)?d/([A-Za-z0-9_-]+)(?:/edit)?/?(?:\?([^#]*))?(?:#(.*))?"
)
_TAB_ID = re.compile(r"t\.[A-Za-z0-9_-]+")
_CONTEXT_QUERY_PARAMS = frozenset(
    {
        "authuser",
        "ouid",
        "pli",
        "sharing",
        "usp",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


@dataclass(frozen=True)
class GoogleDocsReference:
    document_id: str
    tab_id: str | None
    canonical_url: str


def _tab_id(params: list[tuple[str, str]]) -> str | None:
    tabs = [value for key, value in params if key == "tab"]
    if not tabs:
        return None
    if len(tabs) != 1 or not _TAB_ID.fullmatch(tabs[0]):
        raise ValueError("Invalid Google Docs tab")
    return tabs[0]


def validate_google_docs_url(url: str) -> GoogleDocsReference:
    """Canonicalize editor/tab links without discarding unsupported navigation targets."""
    url = url.strip()
    origin_length = len(_GOOGLE_DOCS_ORIGIN)
    # URL parsers may repair malformed paths or strip control characters.
    if (
        url[:origin_length].lower() != _GOOGLE_DOCS_ORIGIN
        or any(char.isspace() or ord(char) < 32 or char in "\\\x7f\ufeff" for char in url)
        or (match := _EDITOR_PATH.fullmatch(url[origin_length:])) is None
    ):
        raise ValueError("Expected an HTTPS Google Docs editor URL")

    document_id, query, fragment = match.groups()
    query_params = parse_qsl(query or "", keep_blank_values=True)
    fragment_params = parse_qsl(fragment or "", keep_blank_values=True)
    if any(key != "tab" and key not in _CONTEXT_QUERY_PARAMS for key, _ in query_params) or (
        fragment and (not fragment_params or any(key != "tab" for key, _ in fragment_params))
    ):
        raise ValueError("This Google Docs target must be opened in Google Docs")

    query_tab = _tab_id(query_params)
    fragment_tab = _tab_id(fragment_params)
    if query_tab and fragment_tab and query_tab != fragment_tab:
        raise ValueError("This Google Docs target must be opened in Google Docs")

    tab_id = query_tab or fragment_tab
    canonical_url = f"{_GOOGLE_DOCS_ORIGIN}/document/d/{document_id}/edit"
    if tab_id is not None:
        canonical_url += f"?tab={tab_id}"
    if len(canonical_url) > 2048:
        raise ValueError("Google Docs URL exceeds 2048 characters")
    return GoogleDocsReference(document_id, tab_id, canonical_url)


class SessionDocumentEntry(BaseModel):
    """An explicitly linked URL and server-generated Unix microsecond timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    document_url: str = Field(max_length=2048)
    added_at: int = Field(ge=1, le=2**53 - 1, strict=True)

    @field_validator("document_url")
    @classmethod
    def supported_url(cls, value: str) -> str:
        validate_google_docs_url(value)
        return value.strip()


def ordered_session_documents(entries: list[SessionDocumentEntry]) -> list[SessionDocumentEntry]:
    """Break equal addition times by document identity consistently across replicas."""
    return sorted(
        entries,
        key=lambda entry: (
            -entry.added_at,
            validate_google_docs_url(entry.document_url).document_id,
        ),
    )
