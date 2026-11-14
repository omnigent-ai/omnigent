# Data-file table preview

CSV, TSV, Excel (`.xlsx`) and Parquet files open as a read-only table in the file viewer. The table shows a summary, each column's inferred type, and up to the first 1,000 rows, with sorting, per-column filters, resizable columns, a sticky header, find, keyboard cell navigation, and copying a cell or row. For CSV and TSV the Source toggle shows the raw text; Download fetches the whole file of any format.

## Sub-features

- `table-open`: `.csv` and `.tsv` files open on the table; the shared Preview ↔ Source toggle switches to the raw text and back.
- `table-summary`: row count, column count, the delimiter (comma, semicolon, tab or pipe, detected from the first lines; always tab for `.tsv`), and the encoding when it isn't UTF-8.
- `table-types`: each column is labeled number, date or text from its non-empty cells; number columns align right.
- `table-sort`: clicking a column header sorts ascending, then descending, then back to file order. Numbers and dates sort by value (a percentage as its fraction, so 25% sorts before 0.5), text naturally; empty cells stay last. A date or time without an offset sorts as UTC, so a date comes before times later that day in any time zone.
- `table-filter`: each column has a filter that keeps rows whose cell contains the text, case-insensitively; the summary shows how many rows match.
- `table-copy`: clicking a cell selects it and offers Copy cell and Copy row (tab-separated).
- `table-bounds`: only the first 1,000 rows, the first 500 columns and 200,000 cells (within a text budget) are kept, so a wide file keeps fewer rows. A note says "Showing the first N of M rows" and, for a wide file, "the first 500 of M columns", counting the widest row anywhere in the file. When the server sent only the start of a large file, the count reads "At least N rows" and the shared banner says the file is truncated.
- `table-encoding`: files the server sends as base64 (not valid UTF-8) are decoded in the browser: UTF-16 by its byte-order mark, else UTF-8, else Windows-1252.
- `table-excel`: an `.xlsx` workbook shows a tab per visible sheet (hidden sheets are skipped; arrow keys move between tabs). Dates show as ISO dates or times (Excel's 1900 calendar, phantom 1900-02-29 included), booleans as TRUE/FALSE, formulas as their saved result, or the formula text when the writer saved none. An empty sheet says so. A cell formatted as a time shows only the time, midnight included.
- `table-parquet`: a `.parquet` file is read in the browser, with the row count taken from its footer. Snappy-compressed and uncompressed files are read; another codec (e.g. ZSTD, Polars' default) is named in a message offering Download. When the nested columns (lists, structs, maps) of the first rows hold more than 10 million values, those columns show "(too many values to preview)" while the flat columns still fill in.
- `table-binary-open`: Excel and Parquet files always open on the table, from any entry point: there is no Source toggle, diff or line view, even for a changed file or a `?diff=1` or line link.
- `table-binary-limits`: Excel and Parquet need the end of the file, so one larger than the server's 10 MiB preview limit shows a message offering Download instead of a table. Both keep the same column, cell and text caps as CSV; a value repeated down a sheet counts toward the text budget each time it shows, column names are cut at 1,000 characters, and a note says when text was cut short. A workbook's parts may inflate to 64 MiB each and 256 MiB together, whatever sizes they declare. A browser without raw-deflate decompression gets a message offering Download.

## How to get to it (user POV)

- Files: open the Files panel, choose Explore or Changes, and select a `.csv`, `.tsv`, `.xlsx` or `.parquet` file.
- Chat file link: click a link to a workspace data file in an agent's reply.
- Deep link: open a conversation URL with `?file=results.csv`.
- Tool row: click the path on an agent's Read, Write or Edit row for a data file, including one it just created; absolute paths open too.
- Agent API: ask the agent to use `ui_open_file` with a workspace-relative data-file path, or call the [file-opening endpoint](../docs/agent-ui-api.md). Only current session viewers (the selected pane in grids) open it.
- Grid: in a session grid, the same links and Files panel open the file in that pane's viewer.
- Inbox: Open file on a comment left on a CSV or TSV deep-links the comment, so the file opens in Source with the comment shown; View preview switches to the table.
- Content search: a match inside a CSV opens the file in Source at the matching line; View preview switches to the table.
- Comments: a `?comment=` link to a CSV or TSV, or picking one of its comments in the Comments panel, opens Source, where comment anchors show; View preview switches back to the table.
- Find: with the table open, press Cmd/Ctrl+F; matches refresh after sorting, filtering or a file change, and the table scrolls sideways to a match in a hidden column.
- Keyboard: Tab reaches the table as one stop; arrow keys and Home/End move between cells, and focusing a cell selects it for Copy cell and Copy row. A column's resize handle takes Left and Right and reports its width (64–2,048 pixels) to assistive technology.
- Mobile web: the same entry points at a narrow viewport; the table scrolls horizontally.

## Driving it with the repro environment

Preconditions: install this checkout's web dependencies. Component checks need nothing else; the browser test also needs a build of this checkout and its Playwright Chromium, and runs against a sealed mock backend, so no server is started.

- Component checks cover parsing, delimiter and encoding detection, type inference, bounds, sorting, filtering, copying, the Excel and Parquet readers and routing: `pnpm --filter web test src/shell/dataTable.test.ts src/shell/DataTablePreview.test.tsx src/shell/xlsx.test.ts src/shell/parquet.test.ts src/shell/SpreadsheetPreview.test.tsx src/shell/PreviewSearchBar.test.tsx src/shell/CodeViewer.test.tsx src/shell/FileViewer.test.tsx`. The Excel and Parquet fixtures were written with openpyxl and pyarrow; the prefixed, damaged and shared-string workbooks are hand-built zips.
- Real Chromium (sticky header, resizing, phone width, and the Excel and Parquet readers with the browser's own decompression): `uv run --no-sync pytest tests/browser_ui/files/test_data_table_preview.py --browser-ui-skip-build`.
- Human check: with a runner online, ask the agent to write a CSV of a few thousand rows to the workspace, including a quoted field with a comma and a blank cell. Open it from Files and from the agent's file link. Sort a number column both ways, filter a text column, copy a cell and a row, switch to Source and back, and download it. Repeat with a semicolon-separated export and a Latin-1 file containing `é`. Then ask it to save the same data as a multi-sheet `.xlsx` (with pandas or openpyxl) and as `.parquet`; open each, switch sheets, and check dates and the row count.

## Gotchas

- Sorting and filtering apply to the rows the preview kept, not the whole file.
- The server sends at most the first 10 MiB of a file; past that, the row count is a lower bound.
- Older binary `.xls` workbooks and password-protected workbooks aren't read; they show a message.
- Parquet files compressed with anything but Snappy (or uncompressed) aren't read yet; the message names the codec.
- A Latin-1 file still shows the binary notice in Source view, because the raw view needs UTF-8 text.
