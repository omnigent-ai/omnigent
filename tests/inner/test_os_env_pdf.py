"""Unit tests for PDF page reading in the OS environment."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from omnigent.inner import os_env_pdf
from omnigent.inner.os_env_pdf import (
    PDF_RENDER_MAX_EDGE_PX,
    is_pdf_file,
    parse_page_range,
    read_pdf_pages,
)


def make_pdf(directory: Path, page_texts: list[str], name: str = "doc.pdf") -> Path:
    """Write a minimal PDF with one Helvetica text line per page."""
    page_count = len(page_texts)
    font_id = 3 + 2 * page_count
    objects = [
        "<</Type/Catalog/Pages 2 0 R>>",
        "<</Type/Pages/Kids[{}]/Count {}>>".format(
            " ".join(f"{3 + 2 * index} 0 R" for index in range(page_count)), page_count
        ),
    ]
    for index, text in enumerate(page_texts):
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET" if text else ""
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            f"/Resources<</Font<</F1 {font_id} 0 R>>>>/Contents {4 + 2 * index} 0 R>>"
        )
        objects.append(f"<</Length {len(stream)}>>stream\n{stream}\nendstream")
    objects.append("<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    body = "".join(f"{number} 0 obj{obj}\nendobj\n" for number, obj in enumerate(objects, 1))
    path = directory / name
    # The binary comment line marks the file binary, as real PDFs do.
    path.write_text(
        f"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n{body}trailer<</Root 1 0 R>>\n%%EOF\n", encoding="latin-1"
    )
    return path


def test_is_pdf_file_checks_header(tmp_path: Path) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello\n")
    assert is_pdf_file(make_pdf(tmp_path, ["Alpha"]))
    assert not is_pdf_file(text_file)


def test_parse_page_range_single_and_span() -> None:
    assert parse_page_range("3", 10) == [3]
    assert parse_page_range("2-4", 10) == [2, 3, 4]


@pytest.mark.parametrize(
    ("spec", "total_pages"),
    [("0", 10), ("5-2", 10), ("8-12", 10), ("abc", 10), ("1-21", 30)],
)
def test_parse_page_range_rejects_invalid(spec: str, total_pages: int) -> None:
    with pytest.raises(ValueError):
        parse_page_range(spec, total_pages)


def test_read_pdf_pages_whole_small_document(tmp_path: Path) -> None:
    result = read_pdf_pages(make_pdf(tmp_path, ["Alpha", "Beta"]), None)
    assert result["total_pages"] == 2
    assert [page["page"] for page in result["pages"]] == [1, 2]
    assert "Alpha" in result["pages"][0]["text"]
    image = Image.open(io.BytesIO(base64.b64decode(result["pages"][0]["image_base64"])))
    assert image.format == "PNG"
    assert max(image.size) <= PDF_RENDER_MAX_EDGE_PX


def test_read_pdf_pages_large_document_requires_range(tmp_path: Path) -> None:
    result = read_pdf_pages(make_pdf(tmp_path, [f"P{number}" for number in range(11)]), None)
    assert result["total_pages"] == 11
    assert "pages" in result["error"]


def test_read_pdf_pages_range_selects_pages(tmp_path: Path) -> None:
    path = make_pdf(tmp_path, [f"P{number}" for number in range(1, 13)])
    result = read_pdf_pages(path, "11-12")
    assert [page["page"] for page in result["pages"]] == [11, 12]
    assert "P11" in result["pages"][0]["text"]


def test_read_pdf_pages_invalid_range_reports_total(tmp_path: Path) -> None:
    result = read_pdf_pages(make_pdf(tmp_path, ["Alpha"]), "2")
    assert result["total_pages"] == 1
    assert "1 pages" in result["error"]


def test_read_pdf_pages_blank_text_marked(tmp_path: Path) -> None:
    result = read_pdf_pages(make_pdf(tmp_path, [""]), None)
    assert result["pages"][0]["text"] == ""


def test_read_pdf_pages_corrupt_file_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a pdf")
    assert read_pdf_pages(path, None)["error"].startswith("Could not open PDF")


def test_pdf_size_cap_matches_attachment_cap() -> None:
    from omnigent.runtime.content_resolver import MAX_PDF_UPLOAD_BYTES

    assert os_env_pdf.MAX_PDF_UPLOAD_BYTES == MAX_PDF_UPLOAD_BYTES


def test_read_pdf_pages_rejects_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_env_pdf, "MAX_PDF_UPLOAD_BYTES", 10)
    assert "too large" in read_pdf_pages(make_pdf(tmp_path, ["Alpha"]), None)["error"]


def test_read_pdf_pages_heavy_page_falls_back_to_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_env_pdf, "PDF_PNG_MAX_BYTES", 1)
    page = read_pdf_pages(make_pdf(tmp_path, ["Alpha"]), None)["pages"][0]
    assert page["media_type"] == "image/jpeg"
    assert Image.open(io.BytesIO(base64.b64decode(page["image_base64"]))).format == "JPEG"


def test_read_pdf_pages_total_image_cap_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_env_pdf, "PDF_MAX_IMAGE_BYTES_PER_READ", 10)
    result = read_pdf_pages(make_pdf(tmp_path, ["Alpha", "Beta"]), None)
    assert result["total_pages"] == 2
    assert "smaller pages range" in result["error"]
