# Data-file table preview

CSV and TSV files open as a read-only table in the file viewer instead of as highlighted text. The table shows a summary, each column's inferred type, and up to the first 1,000 rows, with sorting, per-column filters, resizable columns, a sticky header, and copying a cell or row. The Source toggle shows the raw text and Download fetches the whole file.

## Sub-features

- `table-open`: `.csv` and `.tsv` files open on the table; the shared Preview ↔ Source toggle switches to the raw text and back.
- `table-summary`: row count, column count, the delimiter (comma, semicolon, tab or pipe, detected from the first lines; always tab for `.tsv`), and the encoding when it isn't UTF-8.
- `table-types`: each column is labeled number, date or text from its non-empty cells; number columns align right.
- `table-sort`: clicking a column header sorts ascending, then descending, then back to file order. Numbers and dates sort by value (a percentage as its fraction, so 25% sorts before 0.5), text naturally; empty cells stay last. A date or time without an offset sorts as UTC, so a date comes before times later that day in any time zone.
- `table-filter`: each column has a filter that keeps rows whose cell contains the text, case-insensitively; the summary shows how many rows match.
- `table-copy`: clicking a cell selects it and offers Copy cell and Copy row (tab-separated).
- `table-bounds`: only the first 1,000 rows, the first 500 columns and 200,000 cells (within a text budget) are kept, so a wide file keeps fewer rows. A note says "Showing the first N of M rows" and, for a wide file, "the first 500 of M columns", counting the widest row anywhere in the file. When the server sent only the start of a large file, the count reads "At least N rows" and the shared banner says the file is truncated.
- `table-encoding`: files the server sends as base64 (not valid UTF-8) are decoded in the browser: UTF-16 by its byte-order mark, else UTF-8, else Windows-1252.

## How to get to it (user POV)

- Files: open the Files panel, choose Explore or Changes, and select a `.csv` or `.tsv` file.
- Chat file link: click a link to a workspace `.csv` or `.tsv` file in an agent's reply.
- Deep link: open a conversation URL with `?file=results.csv`.
- Content search: a match inside a CSV opens the file in Source at the matching line; View preview switches to the table.
- Find: with the table open, press Cmd/Ctrl+F; matches refresh after sorting, filtering or a file change, and the table scrolls sideways to a match in a hidden column.
- Keyboard: Tab reaches the table as one stop; arrow keys and Home/End move between cells, and focusing a cell selects it for Copy cell and Copy row. A column's resize handle takes Left and Right and reports its width (64–2,048 pixels) to assistive technology.
- Mobile web: the same entry points at a narrow viewport; the table scrolls horizontally.

## Driving it with the repro environment

Preconditions: install this checkout's web dependencies. Component checks need nothing else; the browser test also needs a build of this checkout and its Playwright Chromium, and runs against a sealed mock backend, so no server is started.

- Component checks cover parsing, delimiter and encoding detection, type inference, bounds, sorting, filtering, copying and routing: `pnpm --filter web test src/shell/dataTable.test.ts src/shell/DataTablePreview.test.tsx src/shell/CodeViewer.test.tsx src/shell/FileViewer.test.tsx`.
- Human check: with a runner online, ask the agent to write a CSV of a few thousand rows to the workspace, including a quoted field with a comma and a blank cell. Open it from Files and from the agent's file link. Sort a number column both ways, filter a text column, copy a cell and a row, switch to Source and back, and download it. Repeat with a semicolon-separated export and a Latin-1 file containing `é`.

## Gotchas

- Sorting and filtering apply to the rows the preview kept, not the whole file.
- The server sends at most the first 10 MiB of a file; past that, the row count is a lower bound.
- Excel (`.xlsx`) and Parquet are not covered yet; they open as before.
- A Latin-1 file still shows the binary notice in Source view, because the raw view needs UTF-8 text.
