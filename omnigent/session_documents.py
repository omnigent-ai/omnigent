"""The label contract for a session's selected Google Doc."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN

GOOGLE_DOCS_SELECTION_LABEL = "ui.google_docs_url"
GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX = "ui.google_docs.document."
_LABEL_KEY_MAX_LEN = 128

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


@dataclass(frozen=True)
class SessionDocuments:
    selected: GoogleDocsReference | None
    documents: dict[str, GoogleDocsReference]


def _tab_id(params: list[tuple[str, str]]) -> str | None:
    tabs = [value for key, value in params if key == "tab"]
    if not tabs:
        return None
    if len(tabs) != 1 or not _TAB_ID.fullmatch(tabs[0]):
        raise ValueError("Invalid Google Docs tab")
    return tabs[0]


def _document_label(document_id: str) -> str:
    return GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + document_id


def validate_google_docs_url(url: str) -> GoogleDocsReference:
    """Canonicalize an editor or tab link that fits in session labels."""
    url = url.strip()
    origin_length = len(_GOOGLE_DOCS_ORIGIN)
    # URL parsers may repair malformed paths or strip control characters.
    if (
        url[:origin_length].lower() != _GOOGLE_DOCS_ORIGIN
        or any(char.isspace() or ord(char) < 32 or char in "\\\x7f" for char in url)
        or (match := _EDITOR_PATH.fullmatch(url[origin_length:])) is None
    ):
        raise ValueError("Expected an HTTPS Google Docs editor URL")

    document_id, query, fragment = match.groups()
    query_params = parse_qsl(query or "", keep_blank_values=True)
    fragment_params = parse_qsl(fragment or "", keep_blank_values=True)
    query_tab = _tab_id(query_params)
    fragment_tab = _tab_id(fragment_params)
    if (
        any(key != "tab" and key not in _CONTEXT_QUERY_PARAMS for key, _ in query_params)
        or (fragment and (fragment_tab is None or len(fragment_params) != 1))
        or (query_tab and fragment_tab and query_tab != fragment_tab)
    ):
        raise ValueError("This Google Docs target must be opened in Google Docs")

    tab_id = query_tab or fragment_tab
    canonical_url = f"{_GOOGLE_DOCS_ORIGIN}/document/d/{document_id}/edit"
    if tab_id is not None:
        canonical_url += f"?tab={tab_id}"
    if len(_document_label(document_id)) > _LABEL_KEY_MAX_LEN:
        raise ValueError(f"Google Docs label key exceeds {_LABEL_KEY_MAX_LEN} characters")
    if len(canonical_url) > LABEL_VALUE_MAX_LEN:
        raise ValueError(f"Google Docs URL exceeds {LABEL_VALUE_MAX_LEN} characters")
    return GoogleDocsReference(document_id, tab_id, canonical_url)


def _read_reference(url: str | None) -> GoogleDocsReference | None:
    if not url:
        return None
    try:
        return validate_google_docs_url(url)
    except ValueError:
        return None


def read_session_documents(labels: Mapping[str, str]) -> SessionDocuments:
    """Read selected-only legacy labels and matching document entries."""
    documents: dict[str, GoogleDocsReference] = {}
    for key, url in labels.items():
        if key.startswith(GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX):
            reference = _read_reference(url)
            if reference is not None and key == _document_label(reference.document_id):
                documents[reference.document_id] = reference

    selected = _read_reference(labels.get(GOOGLE_DOCS_SELECTION_LABEL))
    if selected is not None:
        documents[selected.document_id] = selected
    return SessionDocuments(selected, documents)


def build_google_docs_label_patch(labels: Mapping[str, str], url: str | None) -> dict[str, str]:
    """Build the atomic label update for an explicit save. None unlinks."""
    selected = validate_google_docs_url(url) if url is not None else None
    previous = _read_reference(labels.get(GOOGLE_DOCS_SELECTION_LABEL))
    patch = {GOOGLE_DOCS_SELECTION_LABEL: selected.canonical_url if selected else ""}
    if previous is not None and (selected is None or previous.document_id != selected.document_id):
        previous_key = _document_label(previous.document_id)
        # Legacy selections can lack a document row entirely.
        if labels.get(previous_key):
            patch[previous_key] = ""
    if selected is not None:
        patch[_document_label(selected.document_id)] = selected.canonical_url
    return patch
