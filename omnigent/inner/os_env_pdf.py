"""Read PDF pages as extracted text plus a rendered PNG for the agent read tool.

Mirrors Claude Code's ``Read``: short PDFs are returned whole, longer ones
need an explicit page range, and each call is capped so a single read cannot
flood the model's context.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from omnigent.util.json_types import JsonObject

PDF_WHOLE_DOCUMENT_MAX_PAGES: int = 10
# Same cap as ``content_resolver.MAX_PDF_UPLOAD_BYTES``; not imported to keep the helper light.
MAX_PDF_UPLOAD_BYTES: int = 20 * 1024 * 1024
PDF_MAX_PAGES_PER_READ: int = 20
# Anthropic downscales images past this edge, so larger renders only add bytes.
PDF_RENDER_MAX_EDGE_PX: int = 1568
# Scans and photos compress badly as PNG; past this size a page is sent as JPEG.
PDF_PNG_MAX_BYTES: int = 1024 * 1024
# Under the Claude SDK's 10 MiB per-message cap, which the echoed tool result must fit.
PDF_MAX_IMAGE_BYTES_PER_READ: int = 8 * 1024 * 1024
_JPEG_QUALITY = 80

_PDF_MAGIC = b"%PDF-"


def is_pdf_file(path: Path) -> bool:
    """Return whether *path* starts with the PDF header.

    :param path: File to sniff.
    :returns: ``True`` when the first bytes are ``%PDF-``.
    """
    with path.open("rb") as handle:
        return handle.read(len(_PDF_MAGIC)) == _PDF_MAGIC


def parse_page_range(spec: str, total_pages: int) -> list[int]:
    """Parse a 1-indexed inclusive page range such as ``"3"`` or ``"1-5"``.

    :param spec: Page range text from the model.
    :param total_pages: Page count of the document.
    :returns: The selected page numbers, in order.
    :raises ValueError: For malformed, empty, out-of-bounds or oversized ranges.
    """
    first_text, _, last_text = spec.strip().partition("-")
    try:
        first = int(first_text)
        last = int(last_text) if last_text else first
    except ValueError:
        raise ValueError(f'Invalid pages {spec!r}; use e.g. "3" or "1-5".') from None
    if first < 1 or last < first or last > total_pages:
        raise ValueError(f"Invalid pages {spec!r}; this PDF has {total_pages} pages.")
    if last - first + 1 > PDF_MAX_PAGES_PER_READ:
        raise ValueError(f"Invalid pages {spec!r}; at most {PDF_MAX_PAGES_PER_READ} per call.")
    return list(range(first, last + 1))


def read_pdf_pages(path: Path, pages: str | None) -> JsonObject:
    """Extract text and render a PNG for each requested page of a PDF.

    :param path: Absolute path of the PDF.
    :param pages: Page range, or ``None`` for the whole (short) document.
    :returns: ``{"path", "total_pages", "pages": [{"page", "text",
        "media_type", "image_base64"}]}``, or ``{"error"}`` (with ``total_pages`` once known).
    """
    size = path.stat().st_size
    if size > MAX_PDF_UPLOAD_BYTES:
        return {"error": f"PDF too large ({size} bytes; limit {MAX_PDF_UPLOAD_BYTES})."}
    # Lazy: the helper runtime must start even when optional native wheels fail.
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        return {"error": f"Could not open PDF: {exc}"}
    try:
        total_pages = len(document)
        if pages is None:
            if total_pages > PDF_WHOLE_DOCUMENT_MAX_PAGES:
                return {
                    "error": (
                        f"This PDF has {total_pages} pages. Pass pages (e.g. "
                        f'"1-5"); at most {PDF_MAX_PAGES_PER_READ} pages per call.'
                    ),
                    "total_pages": total_pages,
                }
            selected = list(range(1, total_pages + 1))
        else:
            try:
                selected = parse_page_range(pages, total_pages)
            except ValueError as exc:
                return {"error": str(exc), "total_pages": total_pages}
        rendered: list[JsonObject] = []
        image_bytes = 0
        for number in selected:
            page = {"page": number, **_render_page(document[number - 1])}
            image_bytes += len(str(page["image_base64"]))
            if image_bytes > PDF_MAX_IMAGE_BYTES_PER_READ:
                return {
                    "error": (
                        f"Pages {selected[0]}-{number} exceed the "
                        f"{PDF_MAX_IMAGE_BYTES_PER_READ} byte image limit; "
                        "pass a smaller pages range."
                    ),
                    "total_pages": total_pages,
                }
            rendered.append(page)
    except pdfium.PdfiumError as exc:
        return {"error": f"Could not read PDF: {exc}"}
    finally:
        document.close()
    return {"path": str(path), "total_pages": total_pages, "pages": rendered}


def _render_page(page: object) -> JsonObject:
    """Return one page's text and base64 PNG (JPEG when large), closing the page."""
    import pypdfium2 as pdfium

    assert isinstance(page, pdfium.PdfPage)
    try:
        text_page = page.get_textpage()
        try:
            text = text_page.get_text_range().strip()
        finally:
            text_page.close()
        scale = PDF_RENDER_MAX_EDGE_PX / max(page.get_size())
        image = page.render(scale=scale).to_pil()
    finally:
        page.close()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    media_type = "image/png"
    if buffer.tell() > PDF_PNG_MAX_BYTES:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=_JPEG_QUALITY)
        media_type = "image/jpeg"
    return {
        "text": text,
        "media_type": media_type,
        "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }
