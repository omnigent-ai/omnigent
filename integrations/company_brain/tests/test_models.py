from datetime import UTC, datetime

import pytest
from omnigent_company_brain.models import (
    BrainDocumentV1,
    normalize_markdown,
    sha256_text,
    stable_document_path,
)
from pydantic import ValidationError


def _document(**overrides: object) -> BrainDocumentV1:
    markdown = normalize_markdown(
        '---\r\nsource_url: "https://www.notion.so/page-abc"\r\n---\r\n\r\n'
        "# Security review\r\n\r\nSource facts.  \r\n"
    )
    values: dict[str, object] = {
        "provider": "notion",
        "connection_id": "connection-1",
        "external_resource_id": "page/abc",
        "stable_path": stable_document_path("notion", "connection-1", "page/abc"),
        "title": "Security review",
        "markdown": markdown,
        "canonical_source_url": "https://www.notion.so/page-abc",
        "source_created_at": datetime(2026, 8, 1, tzinfo=UTC),
        "source_modified_at": datetime(2026, 8, 25, tzinfo=UTC),
        "content_sha256": sha256_text(markdown),
        "raw_object_reference": ".raw/notion/page-abc.json",
        "raw_sha256": "a" * 64,
        "transform_schema_version": "notion-page.v1",
    }
    values.update(overrides)
    return BrainDocumentV1.model_validate(values)


def test_document_accepts_deterministic_org_shared_record() -> None:
    document = _document()

    assert document.visibility_class == "org-shared"
    assert document.deletion_state == "active"
    assert document.markdown.endswith("# Security review\n\nSource facts.\n")
    assert document.stable_path == stable_document_path("notion", "connection-1", "page/abc")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("visibility_class", "private"),
        ("content_sha256", "0" * 64),
        ("stable_path", "sources/notion/../../token.md"),
        ("raw_object_reference", ".raw/notion/../../token.json"),
        ("canonical_source_url", "http://www.notion.so/page-abc"),
        ("source_modified_at", datetime(2026, 8, 25, tzinfo=UTC).replace(tzinfo=None)),
    ],
)
def test_document_rejects_records_outside_contract(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _document(**{field: value})


def test_document_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _document(access_token="must-not-be-stored")


def test_active_document_requires_resolvable_source_citation() -> None:
    markdown = normalize_markdown("# Security review\n\nSource facts.\n")

    with pytest.raises(ValidationError, match="canonical_source_url"):
        _document(markdown=markdown, content_sha256=sha256_text(markdown))


def test_stable_path_changes_by_connection_but_not_title() -> None:
    first = stable_document_path("slack", "connection-a", "1712345.001")
    replay = stable_document_path("slack", "connection-a", "1712345.001")
    other_connection = stable_document_path("slack", "connection-b", "1712345.001")

    assert first == replay
    assert first != other_connection
