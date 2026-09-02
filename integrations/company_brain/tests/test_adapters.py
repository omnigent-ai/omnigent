import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import omnigent_company_brain.adapters.google as google_adapter
import pytest
from omnigent_company_brain.adapters import (
    OrgSharedRequiredError,
    transform_google_binary_file,
    transform_google_document,
    transform_google_sheet,
    transform_google_slides,
    transform_notion_page,
    transform_slack_thread,
)
from openpyxl import Workbook

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _golden(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_google_document_matches_golden_markdown() -> None:
    fixture = _fixture("google_document.json")

    result = transform_google_document(
        connection_id=str(fixture["connection_id"]),
        file=fixture["file"],
        exported_markdown=str(fixture["exported_markdown"]),
        org_shared=True,
    )

    assert result.document.markdown == _golden("google_document.md")


def test_google_sheet_matches_golden_markdown() -> None:
    fixture = _fixture("google_document.json")
    file = {**fixture["file"], "id": "sheet-123", "name": "Risk owners"}

    result = transform_google_sheet(
        connection_id=str(fixture["connection_id"]),
        file=file,
        exported_csv='Risk,Owner\nSecurity,Ada\nPrivacy,"Ben | Jo"\n',
        org_shared=True,
    )

    assert result.document.markdown == _golden("google_sheet.md")
    assert result.raw_bytes == b'Risk,Owner\nSecurity,Ada\nPrivacy,"Ben | Jo"\n'
    assert result.raw_object_key is not None
    assert result.raw_object_key.endswith(".csv")


def test_google_slides_matches_golden_markdown() -> None:
    fixture = _fixture("google_document.json")
    file = {**fixture["file"], "id": "slides-123", "name": "Pilot briefing"}

    result = transform_google_slides(
        connection_id=str(fixture["connection_id"]),
        file=file,
        exported_text="Launch plan\nOwner: Ada\fRisks\nVendor delay",
        org_shared=True,
    )

    assert result.document.markdown == _golden("google_slides.md")


def test_google_binary_xlsx_matches_golden_markdown() -> None:
    fixture = _fixture("google_document.json")
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["Risk", "Owner"])
    sheet.append(["Privacy", "Ada"])
    content = BytesIO()
    workbook.save(content)
    file = {
        **fixture["file"],
        "id": "xlsx-123",
        "name": "Risk workbook.xlsx",
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    result = transform_google_binary_file(
        connection_id=str(fixture["connection_id"]),
        file=file,
        content=content.getvalue(),
        org_shared=True,
    )

    assert result.document.markdown == _golden("google_binary_xlsx.md")
    assert result.raw_bytes == content.getvalue()
    assert result.raw_object_key is not None
    assert result.raw_object_key.endswith(".xlsx")


def test_google_openxml_expansion_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture("google_document.json")
    content = BytesIO()
    with zipfile.ZipFile(content, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", "x" * 1024)
    file = {
        **fixture["file"],
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    monkeypatch.setattr(google_adapter, "GOOGLE_OPENXML_MAX_UNCOMPRESSED_BYTES", 100)

    with pytest.raises(ValueError, match="expanded-content limit"):
        transform_google_binary_file(
            connection_id=str(fixture["connection_id"]),
            file=file,
            content=content.getvalue(),
            org_shared=True,
        )


def test_slack_thread_matches_golden_markdown() -> None:
    fixture = _fixture("slack_thread.json")

    result = transform_slack_thread(
        connection_id=str(fixture["connection_id"]),
        workspace_domain=str(fixture["workspace_domain"]),
        channel=fixture["channel"],
        messages=fixture["messages"],
        users=fixture["users"],
    )

    assert result.document.markdown == _golden("slack_thread.md")


def test_notion_page_matches_golden_markdown() -> None:
    fixture = _fixture("notion_page.json")

    result = transform_notion_page(
        connection_id=str(fixture["connection_id"]),
        page=fixture["page"],
        blocks=fixture["blocks"],
        org_shared=True,
    )

    assert result.document.markdown == _golden("notion_page.md")


def test_adapters_reject_content_outside_org_shared_scope() -> None:
    google = _fixture("google_document.json")
    notion = _fixture("notion_page.json")
    slack = _fixture("slack_thread.json")

    with pytest.raises(OrgSharedRequiredError):
        transform_google_document(
            connection_id=str(google["connection_id"]),
            file=google["file"],
            exported_markdown=str(google["exported_markdown"]),
            org_shared=False,
        )
    with pytest.raises(OrgSharedRequiredError):
        transform_notion_page(
            connection_id=str(notion["connection_id"]),
            page=notion["page"],
            blocks=notion["blocks"],
            org_shared=False,
        )
    private_channel = {**slack["channel"], "is_private": True}
    with pytest.raises(OrgSharedRequiredError):
        transform_slack_thread(
            connection_id=str(slack["connection_id"]),
            workspace_domain=str(slack["workspace_domain"]),
            channel=private_channel,
            messages=slack["messages"],
            users=slack["users"],
        )
