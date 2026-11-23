"""The backend and browser share supported Google Docs URL fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.session_documents import (
    GoogleDocsReference,
    SessionDocumentEntry,
    ordered_session_documents,
    validate_google_docs_url,
)

_EDITOR = "https://docs.google.com/document/d/"
_URL_CASES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "web/src/lib/__fixtures__/googleDocsUrls.json"
    ).read_text()
)


@pytest.mark.parametrize("case", _URL_CASES, ids=[case["name"] for case in _URL_CASES])
def test_editor_url_contract(case: dict[str, str]) -> None:
    if case["kind"] == "embed":
        canonical_url = case["canonicalUrl"]
        assert validate_google_docs_url(case["url"]) == GoogleDocsReference(
            "Doc_1-abc", canonical_url.partition("?tab=")[2] or None, canonical_url
        )
        assert (
            SessionDocumentEntry(document_url=case["url"], added_at=1).document_url == case["url"]
        )
    else:
        with pytest.raises(ValueError):
            validate_google_docs_url(case["url"])


def test_original_url_bounds_and_deterministic_order() -> None:
    with pytest.raises(ValueError, match="2048"):
        validate_google_docs_url(_EDITOR + "a" * 2048 + "/edit")
    with pytest.raises(ValueError):
        SessionDocumentEntry(document_url=_EDITOR + "a/edit?usp=" + "x" * 2048, added_at=1)
    entries = [
        SessionDocumentEntry(document_url=_EDITOR + name + "/edit", added_at=stamp)
        for name, stamp in [("a", 1), ("B", 1), ("new", 2)]
    ]
    assert [entry.document_url for entry in ordered_session_documents(entries)] == [
        _EDITOR + name + "/edit" for name in ["new", "B", "a"]
    ]


@pytest.mark.parametrize("stamp", [0, -1, 2**53, 1.5, "1", True])
def test_timestamps_are_positive_safe_integers(stamp: object) -> None:
    with pytest.raises(ValueError):
        SessionDocumentEntry(document_url=_EDITOR + "a/edit", added_at=stamp)
