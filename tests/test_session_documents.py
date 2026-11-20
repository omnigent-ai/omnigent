"""Unit tests for the session document label contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.session_documents import (
    GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX,
    GOOGLE_DOCS_SELECTION_LABEL,
    GoogleDocsReference,
    SessionDocuments,
    build_google_docs_label_patch,
    read_session_documents,
    validate_google_docs_url,
)

_EDITOR = "https://docs.google.com/document/d/"
_DOC_A = f"{_EDITOR}A/edit?tab=t.0"
_DOC_B = f"{_EDITOR}B/edit?tab=t.two"
_DOC_C = f"{_EDITOR}C/edit"
_ENTRY_A = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "A"
_ENTRY_B = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "B"
_ENTRY_C = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "C"
_URL_CASES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "web/src/lib/__fixtures__/googleDocsUrls.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _URL_CASES, ids=[case["name"] for case in _URL_CASES])
def test_editor_url_contract(case: dict[str, str]) -> None:
    if case["kind"] == "embed":
        canonical_url = case["canonicalUrl"]
        tab_id = canonical_url.partition("?tab=")[2] or None
        assert validate_google_docs_url(case["url"]) == GoogleDocsReference(
            "Doc_1-abc", tab_id, canonical_url
        )
    else:
        match = "must be opened in Google Docs" if case["kind"] == "external" else None
        with pytest.raises(ValueError, match=match):
            validate_google_docs_url(case["url"])


def test_label_bounds_are_checked_before_writing() -> None:
    document_id = "a" * (128 - len(GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX))
    url = f"{_EDITOR}{document_id}/edit?tab=t."
    valid_url = url.ljust(256, "b")

    assert build_google_docs_label_patch({}, valid_url) == {
        GOOGLE_DOCS_SELECTION_LABEL: valid_url,
        GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + document_id: valid_url,
    }
    with pytest.raises(ValueError, match="URL exceeds 256"):
        build_google_docs_label_patch({}, valid_url + "b")
    with pytest.raises(ValueError, match="label key exceeds 128"):
        build_google_docs_label_patch({}, f"{_EDITOR}{document_id}a/edit")


def test_legacy_selection_is_read_and_normalized_on_explicit_save() -> None:
    labels = {GOOGLE_DOCS_SELECTION_LABEL: f"{_EDITOR}A/edit?usp=sharing#tab=t.0"}
    reference = GoogleDocsReference("A", "t.0", _DOC_A)

    assert read_session_documents(labels) == SessionDocuments(reference, {"A": reference})
    assert labels == {GOOGLE_DOCS_SELECTION_LABEL: f"{_EDITOR}A/edit?usp=sharing#tab=t.0"}
    assert build_google_docs_label_patch(labels, _DOC_A) == {
        GOOGLE_DOCS_SELECTION_LABEL: _DOC_A,
        _ENTRY_A: _DOC_A,
    }
    assert build_google_docs_label_patch(labels, None) == {GOOGLE_DOCS_SELECTION_LABEL: ""}


def test_label_patch_replaces_or_clears_only_the_selected_entry() -> None:
    labels = {
        GOOGLE_DOCS_SELECTION_LABEL: _DOC_A,
        _ENTRY_A: _DOC_A,
        _ENTRY_C: _DOC_C,
        "other.label": "retained",
    }

    change = build_google_docs_label_patch(labels, _DOC_B)
    assert change == {GOOGLE_DOCS_SELECTION_LABEL: _DOC_B, _ENTRY_A: "", _ENTRY_B: _DOC_B}
    changed = labels | change
    changed_state = read_session_documents(changed)
    assert changed_state.selected == GoogleDocsReference("B", "t.two", _DOC_B)
    assert changed_state.documents == {
        "B": GoogleDocsReference("B", "t.two", _DOC_B),
        "C": GoogleDocsReference("C", None, _DOC_C),
    }

    clear = build_google_docs_label_patch(changed, None)
    assert clear == {GOOGLE_DOCS_SELECTION_LABEL: "", _ENTRY_B: ""}
    cleared = changed | clear
    assert cleared["other.label"] == "retained"
    assert read_session_documents(cleared) == SessionDocuments(
        None, {"C": GoogleDocsReference("C", None, _DOC_C)}
    )


def test_selection_is_authoritative_and_ignores_mismatched_or_invalid_rows() -> None:
    url = f"{_EDITOR}A/edit?tab=t.next"
    labels = {
        GOOGLE_DOCS_SELECTION_LABEL: url,
        _ENTRY_A: _DOC_A,
        _ENTRY_B: _DOC_C,
        _ENTRY_C: _DOC_C + "#heading=h.section",
    }
    reference = GoogleDocsReference("A", "t.next", url)

    assert read_session_documents(labels) == SessionDocuments(reference, {"A": reference})
    assert read_session_documents(labels | {GOOGLE_DOCS_SELECTION_LABEL: ""}) == SessionDocuments(
        None, {"A": GoogleDocsReference("A", "t.0", _DOC_A)}
    )
    assert build_google_docs_label_patch(labels, _DOC_A) == {
        GOOGLE_DOCS_SELECTION_LABEL: _DOC_A,
        _ENTRY_A: _DOC_A,
    }
