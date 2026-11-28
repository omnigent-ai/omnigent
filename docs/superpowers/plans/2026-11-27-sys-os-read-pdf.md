# `sys_os_read` PDF Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `sys_os_read` read PDFs the way Claude Code's `Read` does — text plus a rendered image per page, with a `pages` range — so Claude SDK harness agents can read PDFs without flooding context.

**Architecture:** A new helper module renders the requested pages with `pypdfium2` (text extraction + PNG render) inside the existing os_env helper process, so the sandbox's read checks still apply. `sys_os_read` wraps the pages in the existing MCP image envelope (`encode_mcp_image_result`), which the Claude SDK executor already turns into native MCP `text`/`image` blocks (`omnigent/inner/claude_sdk_executor.py:1469`). History replay already swaps envelope images for placeholders (`omnigent/runtime/prompt.py:_strip_output_image_data`), so only the turn that reads the PDF pays the image cost.

**Tech Stack:** Python, `pypdfium2` (bundled PDFium wheels, no system poppler), Pillow (already a dependency), pytest.

**Spec:** This conversation's requirements; no separate spec file. Requirements are copied into Global Constraints below.

## Global Constraints

- Match Claude Code `Read` limits: a PDF of at most 10 pages is returned whole when `pages` is omitted; a larger PDF requires `pages`; at most 20 pages per call.
- `pages` syntax: `"3"`, `"1-5"`, `"10-20"`; 1-indexed and inclusive.
- Each page is returned as a text block (`--- page N of T ---` header + extracted text) followed by one PNG image block.
- Render each page so its longest edge is at most 1568 px (Anthropic's downscale threshold; larger costs bytes without adding tokens).
- Refuse PDFs larger than `MAX_PDF_UPLOAD_BYTES` (20 MiB, `omnigent/runtime/content_resolver.py:125`) before opening them.
- Only the agent path changes (`max_bytes is None`). The filesystem-service path (`max_bytes` set, used by the web viewer and downloads) keeps returning base64 bytes unchanged.
- Dependency: add `pypdfium2>=4.30,<6` to `pyproject.toml` and lock it in `uv.lock`. Keep the floor at a release older than the mirror embargo, run `just check-internal-mirror` if the proxy is reachable, and never add the `dependency-floor-approved` label.
- Import `pypdfium2` lazily inside the PDF function, not at module top level (the helper runtime must start even from a broken checkout; see `tests/inner/test_os_env_runtime_imports.py`).
- Test fixtures use generic names only (reference policy, `dev/lint/lint_reference_policy.py`).

## Review Focus

1. **Scanned PDF (no text layer):** the page text is empty. Expect the header plus `(no extractable text)` and the image, not an empty block. Tests: `test_read_pdf_pages_blank_text_marked` (Task 1, raw `""`) and `test_sys_os_read_pdf_blank_page_marked` (Task 3, marker).
2. **Encrypted or corrupt PDF:** expect `{"error": "Could not open PDF: ..."}`, not a crash or a hung helper. Test: `test_read_pdf_pages_corrupt_file_errors` (Task 1).
3. **Range past the end** (`"8-12"` on a 10-page PDF) or reversed (`"5-2"`): expect an error naming `total_pages`, not silent clamping. Tests: `test_parse_page_range_rejects_*` (Task 1).
4. **`pages` given for a non-PDF file:** expect `{"error": "pages applies only to PDF files"}`. Test: `test_read_impl_pages_on_text_file_errors` (Task 2).
5. **Non-Claude harness gets the envelope:** a harness without image support must not inline base64 as text. The existing consumers (`codex_executor.py:3000`, `copilot_executor.py:256`, `executor.py:637`) already decode the envelope; Task 3's manual check covers the Claude SDK path. Check any other harness that registers `sys_os_read` before merging.

---

### Task 1: PDF page reader module + dependency

**Files:**
- Modify: `pyproject.toml` (dependencies), `uv.lock`
- Create: `omnigent/inner/os_env_pdf.py`
- Create: `tests/inner/test_os_env_pdf.py`

**Interfaces:**
- Produces:
  - `PDF_WHOLE_DOCUMENT_MAX_PAGES: int = 10`
  - `PDF_MAX_PAGES_PER_READ: int = 20`
  - `PDF_RENDER_MAX_EDGE_PX: int = 1568`
  - `is_pdf_file(path: Path) -> bool`: true when the first 5 bytes are `b"%PDF-"`.
  - `parse_page_range(spec: str, total_pages: int) -> list[int]`: 1-indexed page numbers; raises `ValueError` with a model-readable message.
  - `read_pdf_pages(path: Path, pages: str | None) -> OpResult`: on success `{"path": str, "total_pages": int, "pages": [{"page": int, "text": str, "png_base64": str}]}`; on failure `{"error": str}` (plus `"total_pages"` when known).

- [ ] **Step 1: Add the dependency**

Add `"pypdfium2>=4.30,<6"` to `[project].dependencies` in `pyproject.toml`; run `uv lock`. Expected: `uv.lock` gains a `pypdfium2` package entry; no other packages move.

- [ ] **Step 2: Write the failing tests**

Test-local builder `_make_pdf(tmp_path: Path, page_texts: list[str]) -> Path`, writing a minimal multi-page PDF modelled on `_PDF_CONTENT` in `tests/e2e_ui/files/test_pdf_rendering.py:38` (one Helvetica `Tj` per page; pdfium tolerates the missing xref).

```python
def test_parse_page_range_single_and_span() -> None:
    assert parse_page_range("3", 10) == [3]
    assert parse_page_range("2-4", 10) == [2, 3, 4]

@pytest.mark.parametrize("spec", ["0", "5-2", "8-12", "abc", "1-21"])
def test_parse_page_range_rejects_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_page_range(spec, 30 if spec == "1-21" else 10)

def test_read_pdf_pages_whole_small_document(tmp_path: Path) -> None:
    result = read_pdf_pages(_make_pdf(tmp_path, ["Alpha", "Beta"]), None)
    assert result["total_pages"] == 2
    assert [p["page"] for p in result["pages"]] == [1, 2]
    assert "Alpha" in result["pages"][0]["text"]
    image = Image.open(io.BytesIO(base64.b64decode(result["pages"][0]["png_base64"])))
    assert image.format == "PNG" and max(image.size) <= PDF_RENDER_MAX_EDGE_PX

def test_read_pdf_pages_large_document_requires_range(tmp_path: Path) -> None:
    result = read_pdf_pages(_make_pdf(tmp_path, [f"P{n}" for n in range(11)]), None)
    assert result["total_pages"] == 11 and "pages" in result["error"]

def test_read_pdf_pages_range_selects_pages(tmp_path: Path) -> None:
    result = read_pdf_pages(_make_pdf(tmp_path, [f"P{n}" for n in range(1, 13)]), "11-12")
    assert [p["page"] for p in result["pages"]] == [11, 12]
    assert "P11" in result["pages"][0]["text"]

def test_read_pdf_pages_blank_text_marked(tmp_path: Path) -> None:
    result = read_pdf_pages(_make_pdf(tmp_path, [""]), None)
    assert result["pages"][0]["text"] == ""

def test_read_pdf_pages_corrupt_file_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a pdf")
    assert read_pdf_pages(path, None)["error"].startswith("Could not open PDF")

def test_read_pdf_pages_rejects_oversized_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(os_env_pdf, "MAX_PDF_UPLOAD_BYTES", 10)
    assert "too large" in read_pdf_pages(_make_pdf(tmp_path, ["A"]), None)["error"]
```

The `(no extractable text)` marker is added by the tool (Task 3), so the reader returns raw `""`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/inner/test_os_env_pdf.py -v`
Expected: FAIL with `ModuleNotFoundError: omnigent.inner.os_env_pdf`.

- [ ] **Step 4: Implement `omnigent/inner/os_env_pdf.py`**

Check the size with `path.stat().st_size` against `MAX_PDF_UPLOAD_BYTES` before opening. Inside the function, `import pypdfium2 as pdfium`. Open with `pdfium.PdfDocument(path)` and map `pdfium.PdfiumError` to `Could not open PDF: <msg>`. Get text with `page.get_textpage().get_text_range()`. Render with `page.render(scale=PDF_RENDER_MAX_EDGE_PX / max(page.get_size())).to_pil()` and save as PNG into a `BytesIO`. Close pages and the document in `finally`. Error message when the range is missing: `f"This PDF has {total} pages. Pass pages (e.g. \"1-5\"); at most {PDF_MAX_PAGES_PER_READ} pages per call."`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/inner/test_os_env_pdf.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock omnigent/inner/os_env_pdf.py tests/inner/test_os_env_pdf.py
git commit -m "Read PDF pages as text and images for the OS environment"
```

### Task 2: Route PDF reads through the os_env helper

**Files:**
- Modify: `omnigent/inner/os_env.py` (`OSEnvironment.read` ~`:350`, `CallerProcessOSEnvironment.read` ~`:1321`, helper `op == "read"` ~`:1650`, `_read_impl` ~`:2136`)
- Test: `tests/inner/test_os_env.py` (next to `test_read_impl_binary_descriptor_for_agent`, `:247`)

**Interfaces:**
- Consumes: `is_pdf_file`, `read_pdf_pages` (Task 1).
- Produces: `OSEnvironment.read(..., *, regular_only: bool = False, pages: str | None = None) -> OpResult` and `_read_impl(path, offset, limit, max_bytes=None, pages: str | None = None) -> OpResult`. The PDF result is Task 1's `read_pdf_pages` shape.

- [ ] **Step 1: Write the failing tests**

```python
def test_read_impl_pdf_returns_pages_for_agent(tmp_path: Path) -> None:
    result = _read_impl(_make_pdf(tmp_path, ["Alpha"]), 1, None)
    assert result["total_pages"] == 1 and result["pages"][0]["png_base64"]

def test_read_impl_pdf_bytes_unchanged_for_filesystem_service(tmp_path: Path) -> None:
    result = _read_impl(_make_pdf(tmp_path, ["Alpha"]), 1, None, max_bytes=1_000_000)
    assert result["encoding"] == "base64" and "pages" not in result

def test_read_impl_pages_on_text_file_errors(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello\n")
    assert _read_impl(path, 1, None, pages="1") == {"error": "pages applies only to PDF files"}

async def test_caller_process_read_forwards_pages(tmp_path: Path) -> None:
    # Use the existing CallerProcessOSEnvironment fixture/pattern in this file.
    result = await environment.read("doc.pdf", pages="2")
    assert [p["page"] for p in result["pages"]] == [2]
```

Import `_make_pdf` from `tests/inner/test_os_env_pdf.py`, or move it to a shared test helper if the test guide prefers that. Don't duplicate it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/inner/test_os_env.py -k "pdf or pages" -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'pages'`, or the descriptor shape is returned).

- [ ] **Step 3: Implement**

In `_read_impl`, after argument validation and before `_is_binary_file`:
- if `max_bytes is None and is_pdf_file(path)`, return `read_pdf_pages(path, pages)`;
- else if `pages is not None`, return the non-PDF error.

Add `pages` to both `read` signatures, put it in the helper request dict, and forward `request.get("pages")` (str or None) in the helper `read` op. Leave the `regular_only` branch untouched.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/inner/test_os_env.py tests/inner/test_os_env_pdf.py -v`
Expected: all PASS, including the existing binary-descriptor tests.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/os_env.py tests/inner/test_os_env.py
git commit -m "Return PDF pages from the agent read path"
```

### Task 3: Expose `pages` on `sys_os_read` and emit the image envelope

**Files:**
- Modify: `omnigent/tools/builtins/os_env.py` (`_OS_READ_SCHEMA`, `SysOsReadTool.description`, `SysOsReadTool._invoke_async` ~`:225`)
- Test: `tests/tools/builtins/test_os_env.py`

**Interfaces:**
- Consumes: `OSEnvironment.read(..., pages=...)` (Task 2); `encode_mcp_image_result(content, *, is_error) -> str` and `decode_mcp_image_result(value) -> McpImageResult | None` from `omnigent/runtime/mcp_tool_result.py`.
- Produces: for a PDF, `SysOsReadTool` returns the decoded envelope dict `json.loads(encode_mcp_image_result(blocks, is_error=False))`. Blocks, in order:
  1. a text block with `json.dumps({"path", "total_pages", "pages_returned"})`;
  2. for each page, a text block `f"--- page {n} of {total} ---\n{text or '(no extractable text)'}"` followed by `{"type": "image", "mimeType": "image/png", "data": png_base64}`.

  Error and text results are unchanged dicts.

- [ ] **Step 1: Write the failing tests**

```python
async def test_sys_os_read_pdf_returns_image_envelope(...) -> None:
    result = await tool.invoke({"path": "doc.pdf"})  # follow this file's invoke pattern
    decoded = decode_mcp_image_result(result)
    assert decoded is not None and not decoded.is_error
    kinds = [block["type"] for block in decoded.content]
    assert kinds == ["text", "text", "image"]
    assert decoded.content[1]["text"].startswith("--- page 1 of 1 ---")

async def test_sys_os_read_pdf_blank_page_marked(...) -> None:
    ...  # blank-text PDF -> "(no extractable text)" in the page text block

def test_sys_os_read_schema_documents_pages() -> None:
    assert "pages" in SysOsReadTool(...).tool_schema()["parameters"]["properties"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/tools/builtins/test_os_env.py -k pdf -v`
Expected: FAIL (the plain dict is returned; `decode_mcp_image_result` gives `None`).

- [ ] **Step 3: Implement**

Add `"pages"` to `_OS_READ_SCHEMA` with the description `Page range for PDF files (e.g. "1-5", "3", "10-20"). Required for PDFs over 10 pages; at most 20 pages per call.` Change the description to `Read a text file, or a PDF as page text plus page images, from the OS environment.` Forward `pages=kwargs.get("pages")`. When the result has a `"pages"` list, build the envelope; otherwise return as before.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/tools/builtins/test_os_env.py -v`
Expected: all PASS.

- [ ] **Step 5: Manual check on the Claude SDK harness**

Start the dev backend on the shared database: `OMNIGENT_DATABASE_URI="sqlite:///$HOME/.omnigent/chat.db"`. Then, in a `claude-sdk` session with `os_env`:
- put a 3-page PDF in the cwd and ask "Read doc.pdf and summarize page 2". Expect one `sys_os_read` call and a correct summary.
- with a 15-page PDF, expect the model to retry with `pages`.
- send a follow-up turn and confirm that replayed history shows image placeholders, not base64.

- [ ] **Step 6: Commit**

```bash
git add omnigent/tools/builtins/os_env.py tests/tools/builtins/test_os_env.py
git commit -m "Let sys_os_read return PDF pages as text and images"
```
