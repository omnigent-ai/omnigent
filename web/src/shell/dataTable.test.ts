import { describe, expect, it, vi } from "vitest";
import {
  DATA_TABLE_MAX_CELLS,
  DATA_TABLE_MAX_CHARS,
  DATA_TABLE_MAX_NAME_CHARS,
  DATA_TABLE_MAX_COLUMNS,
  DATA_TABLE_MAX_ROWS,
  compareCells,
  decodeDataFile,
  detectDelimiter,
  inferColumnType,
  isDataTablePath,
  parseDataTable,
  tableFromRecords,
  type DataTable,
} from "./dataTable";

/** Base64 of raw bytes, as the server sends a file that isn't UTF-8. */
function base64Of(bytes: number[]): string {
  return btoa(String.fromCharCode(...bytes));
}

describe("isDataTablePath", () => {
  it.each([
    ["results.csv", true],
    ["out/DATA.TSV", true],
    ["notes.csv.md", false],
    ["data.json", false],
    ["csv", false],
  ])("%s → %s", (path, expected) => {
    expect(isDataTablePath(path)).toBe(expected);
  });
});

describe("parseDataTable", () => {
  it("reads quoted fields holding the delimiter, newlines and doubled quotes", () => {
    const text = 'name,note\r\n"Smith, J","said ""hi""\nthen left"\r\nLee,ok\r\n';
    const table = parseDataTable(text, "people.csv");
    expect(table?.header).toEqual(["name", "note"]);
    expect(table?.rows).toEqual([
      ["Smith, J", 'said "hi"\nthen left'],
      ["Lee", "ok"],
    ]);
    expect(table?.totalRows).toBe(2);
  });

  it("skips blank lines and pads short rows, naming extra columns", () => {
    const table = parseDataTable("a,b\n1\n\n2,3,4\n", "x.csv");
    expect(table?.header).toEqual(["a", "b", "column 3"]);
    expect(table?.rows).toEqual([
      ["1", "", ""],
      ["2", "3", "4"],
    ]);
  });

  it("bounds a sparse, wide file by columns and cells, still counting both", () => {
    // One row of delimiters would otherwise widen every row: 1,000 rows × 2,000 columns.
    const text = `h\n${"x\n".repeat(999)}${",".repeat(1999)}\n`;
    const table = parseDataTable(text, "sparse.csv");
    expect(table?.header).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(table?.totalColumns).toBe(2000);
    expect(table?.totalRows).toBe(1000);
    const cells = ((table?.rows.length ?? 0) + 1) * (table?.header.length ?? 0);
    expect(cells).toBeLessThanOrEqual(DATA_TABLE_MAX_CELLS);
    expect(table?.rows.every((row) => row.length === DATA_TABLE_MAX_COLUMNS)).toBe(true);
  });

  it("caps an oversized header's columns", () => {
    const table = parseDataTable(`${"c,".repeat(3000)}c\n1\n`, "wide.csv");
    expect(table?.header).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(table?.totalColumns).toBe(3001);
    expect(table?.rows).toHaveLength(1);
  });

  it("returns null for an empty file", () => {
    expect(parseDataTable("", "empty.csv")).toBeNull();
    expect(parseDataTable("\n\n", "empty.csv")).toBeNull();
  });

  it("keeps the first rows and counts every row and column", () => {
    const rows = Array.from({ length: DATA_TABLE_MAX_ROWS + 500 }, (_, i) => `${i},x`);
    // A row past the kept ones is wider than any kept row.
    const table = parseDataTable(["id,value", ...rows, "a,b,c"].join("\n"), "big.csv");
    expect(table?.rows).toHaveLength(DATA_TABLE_MAX_ROWS);
    expect(table?.rows.at(-1)).toEqual([String(DATA_TABLE_MAX_ROWS - 1), "x"]);
    expect(table?.totalRows).toBe(DATA_TABLE_MAX_ROWS + 501);
    expect(table?.header).toEqual(["id", "value"]);
    expect(table?.totalColumns).toBe(3);
  });

  // The kept rows stay a prefix of the file: once a row doesn't fit, later
  // rows aren't kept either, though their rows and columns still count.
  it.each([
    ["a row that fills the budget", "x".repeat(DATA_TABLE_MAX_CHARS), "wide.csv", ","],
    [
      "one cell bigger than the whole budget",
      "x".repeat(DATA_TABLE_MAX_CHARS * 2),
      "wide.csv",
      ",",
    ],
    [
      "one tab-separated cell bigger than the budget",
      "x".repeat(DATA_TABLE_MAX_CHARS * 2),
      "wide.tsv",
      "\t",
    ],
  ])("stops keeping rows at %s", (_label, wide, path, delimiter) => {
    const table = parseDataTable(`col\nshort\n${[wide, "y", "z"].join(delimiter)}\nlater\n`, path);
    expect(table?.rows).toEqual([["short"]]);
    expect(table?.totalRows).toBe(3);
    expect(table?.totalColumns).toBe(3);
  });

  it("keeps an oversized header as the header, each name cut to the name limit", () => {
    const wideHeader = "h".repeat(DATA_TABLE_MAX_CHARS + 1);
    const table = parseDataTable(`${wideHeader},b\n1,2\n`, "wide.csv");
    // Only the kept part of a name counts, so the rest of the file still fits.
    expect(table?.header).toEqual(["h".repeat(DATA_TABLE_MAX_NAME_CHARS), "b"]);
    expect(table?.rows).toEqual([["1", "2"]]);
    expect(table?.textCut).toBe(true);
    expect(table?.totalRows).toBe(1);
  });

  it("charges a cut header name only what the table keeps of it", () => {
    const name = "h".repeat(1_500_000);
    const row = "r".repeat(600_000);
    const table = parseDataTable(`${name}\n${row}\n`, "x.csv");
    expect(table?.header[0]).toHaveLength(DATA_TABLE_MAX_NAME_CHARS);
    expect(table?.rows).toEqual([[row]]);
  });

  it("doesn't cut a character in half when it cuts a row", () => {
    const cell = `${"x".repeat(DATA_TABLE_MAX_CHARS - 3)}😀😀`;
    const table = tableFromRecords([["a"], [cell]], 1, 1, "test");
    const kept = table?.rows[0]?.[0] ?? "";
    expect(kept.endsWith("x…")).toBe(true);
  });

  // Text after the cut follows the emoji, so a cut that freed room for it would show.
  it.each([
    ["a row", `h\n${"x".repeat(DATA_TABLE_MAX_CHARS - 2)}😀yz\n`, (t: DataTable) => t.rows[0][0]],
    ["a column name", `${"x".repeat(999)}😀yz,b\n1,2\n`, (t: DataTable) => t.header[0]],
  ])(
    "cuts %s before a character it can't keep whole, and keeps nothing after",
    (_l, text, pick) => {
      const kept = pick(parseDataTable(text, "data.csv")!);
      expect(kept).toMatch(/^x+$/);
    },
  );

  it("keeps generated column labels within the text budget", () => {
    const table = parseDataTable(
      `${"h".repeat(DATA_TABLE_MAX_CHARS)}\n${",".repeat(499)}\n`,
      "x.csv",
    );
    const kept = [table?.header ?? [], ...(table?.rows ?? [])].flat().join("").length;
    expect(table?.header).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(kept).toBeLessThanOrEqual(DATA_TABLE_MAX_CHARS);
  });

  it("keeps a CSV's oversized first row cut, as the other readers do", () => {
    const table = parseDataTable(`a,b\n${"x".repeat(DATA_TABLE_MAX_CHARS)},1\n2,3\n`, "x.csv");
    expect(table?.rows).toHaveLength(1);
    expect(table?.rows[0]?.[0]).toHaveLength(DATA_TABLE_MAX_CHARS - "ab".length);
    expect(table?.textCut).toBe(true);
    expect(table?.totalRows).toBe(2);
  });

  it("infers each column's type", () => {
    const text = 'id,when,amount,label\n1,2026-01-02,"1,234.5",a\n2,2026-02-03T10:00:00Z,7%,b\n';
    expect(parseDataTable(text, "t.csv")?.columnTypes).toEqual([
      "number",
      "date",
      "number",
      "text",
    ]);
  });
});

describe("tableFromRecords", () => {
  it("keeps the header and rows within the text budget, however often a value repeats", () => {
    // One string shared by every cell, as a workbook's shared strings or a
    // Parquet dictionary give it: 500 columns × 1,000 rows of it.
    const value = "v".repeat(10_000);
    const row = Array<string>(DATA_TABLE_MAX_COLUMNS).fill(value);
    const records = [row, ...Array.from({ length: DATA_TABLE_MAX_ROWS }, () => row)];

    const table = tableFromRecords(records, DATA_TABLE_MAX_ROWS, DATA_TABLE_MAX_COLUMNS, "test");

    const kept = [table?.header ?? [], ...(table?.rows ?? [])].flat().join("").length;
    expect(kept).toBe(DATA_TABLE_MAX_CHARS);
    // Each name is cut to the name limit; the first row is cut to the rest.
    expect(new Set(table?.header.map((name) => name.length))).toEqual(
      new Set([DATA_TABLE_MAX_NAME_CHARS]),
    );
    expect(table?.rows).toHaveLength(1);
    expect(table?.textCut).toBe(true);
    expect(table?.totalRows).toBe(DATA_TABLE_MAX_ROWS);
  });

  it("keeps a first row bigger than the budget, cutting only the cell that overflows", () => {
    const wide = "x".repeat(DATA_TABLE_MAX_CHARS);
    const table = tableFromRecords(
      [
        ["id", "note", "amount"],
        ["1", wide, "1234"],
        ["2", "b", "5"],
      ],
      2,
      3,
      "test",
    );

    const [id, note, amount] = table?.rows[0] ?? [];
    expect(table?.rows).toHaveLength(1);
    expect(id).toBe("1");
    // The overflowing cell ends in an ellipsis; a cell after it is blank, not shortened.
    expect(note).toHaveLength(DATA_TABLE_MAX_CHARS - "idnoteamount1".length);
    expect(note?.endsWith("…")).toBe(true);
    expect(amount).toBe("");
    expect(table?.textCut).toBe(true);
    expect(table?.totalRows).toBe(2);
  });

  it("labels a blank or whitespace column name and marks no text cut", () => {
    const table = tableFromRecords(
      [
        ["id", "", "  "],
        ["1", "2", "3"],
      ],
      1,
      3,
      "test",
    );
    expect(table?.header).toEqual(["id", "column 2", "column 3"]);
    expect(table?.textCut).toBeUndefined();
  });
});

describe("detectDelimiter", () => {
  it.each([
    ["a;b;c\n1;2;3\n", "x.csv", ";"],
    ["a|b\n1|2\n", "x.csv", "|"],
    ["a\tb\n1\t2\n", "x.csv", "\t"],
    ["a,b\n1,2\n", "x.tsv", "\t"],
    ["only\none\n", "x.csv", ","],
  ])("%j in %s → %j", (text, path, expected) => {
    expect(detectDelimiter(text, path)).toBe(expected);
  });

  it("tolerates a short row", () => {
    expect(detectDelimiter("a;b;c\n1;2;3\n4;5\n", "x.csv")).toBe(";");
  });

  it("decides from the first records only", () => {
    const head = Array.from({ length: 20 }, (_, i) => `${i};x`).join("\n");
    const tail = Array.from({ length: 100 }, () => "a,b,c,d").join("\n");
    expect(detectDelimiter(`${head}\n${tail}`, "x.csv")).toBe(";");
  });

  it("detects the delimiter of a file whose first line is a huge row of semicolons", () => {
    // A row far wider than the column cap is still read; semicolon isn't the fallback.
    expect(detectDelimiter(`${";".repeat(1_000_000)}\n`, "x.csv")).toBe(";");
  });

  it("ignores a delimiter inside quotes", () => {
    // Semicolons split every row; the commas sit inside quoted fields only.
    expect(detectDelimiter('a;b\n"1,5";"2,5"\n"3,1";"4,0"\n', "eu.csv")).toBe(";");
  });
});

describe("decodeDataFile", () => {
  it("passes UTF-8 through without its byte-order mark", () => {
    expect(decodeDataFile("﻿a,b", "utf-8")).toEqual({ text: "a,b", encoding: "utf-8" });
  });

  it("decodes a Latin-1 export the server sent as base64", () => {
    // "café" in Windows-1252: é is 0xE9, which is not valid UTF-8.
    const bytes = [0x63, 0x61, 0x66, 0xe9];
    expect(decodeDataFile(base64Of(bytes), "base64")).toEqual({
      text: "café",
      encoding: "windows-1252",
    });
  });

  it.each([
    ["little-endian", [0xff, 0xfe, 0x61, 0x00, 0x2c, 0x00, 0x62, 0x00], "utf-16le"],
    ["big-endian", [0xfe, 0xff, 0x00, 0x61, 0x00, 0x2c, 0x00, 0x62], "utf-16be"],
  ])("decodes %s UTF-16 marked by its byte-order mark", (_, bytes, encoding) => {
    expect(decodeDataFile(base64Of(bytes), "base64")).toEqual({ text: "a,b", encoding });
  });

  it("prefers UTF-8 for base64 bytes that are valid UTF-8", () => {
    const bytes = [...new TextEncoder().encode("﻿naïve")];
    expect(decodeDataFile(base64Of(bytes), "base64")).toEqual({
      text: "naïve",
      encoding: "utf-8",
    });
  });
});

describe("inferColumnType and compareCells", () => {
  it.each([
    [["1", "-2.5", "1e3", ""], "number"],
    [["2026-01-02", "2026-12-31 08:30"], "date"],
    [["1", "two"], "text"],
    [["", " "], "text"],
  ] as const)("%j → %s", (values, expected) => {
    expect(inferColumnType([...values])).toBe(expected);
  });

  it("orders numbers by value, text naturally, and empty cells last", () => {
    expect(["10", "", "2", "1,000"].sort((a, b) => compareCells(a, b, "number"))).toEqual([
      "2",
      "10",
      "1,000",
      "",
    ]);
    // A percentage is a fraction: 25% is less than a half.
    expect(["1", "25%", "0.5", "150%"].sort((a, b) => compareCells(a, b, "number"))).toEqual([
      "25%",
      "0.5",
      "1",
      "150%",
    ]);
    expect(["item10", "item2"].sort((a, b) => compareCells(a, b, "text"))).toEqual([
      "item2",
      "item10",
    ]);
    expect(
      ["2026-03-01", "", "2025-12-31T23:00:00Z", "2026-01-15"].sort((a, b) =>
        compareCells(a, b, "date"),
      ),
    ).toEqual(["2025-12-31T23:00:00Z", "2026-01-15", "2026-03-01", ""]);
  });

  it("sorts Excel's phantom 1900-02-29 between Feb 28 and Mar 1", () => {
    expect(
      ["1900-03-01", "1900-02-29 12:00:00", "1900-02-28 23:00:00", "1900-02-29"].sort((a, b) =>
        compareCells(a, b, "date"),
      ),
    ).toEqual(["1900-02-28 23:00:00", "1900-02-29", "1900-02-29 12:00:00", "1900-03-01"]);
    // An offset can't move it past Mar 1.
    expect(compareCells("1900-02-29T23:00:00-12:00", "1900-03-01", "date")).toBeLessThan(0);
  });

  it("orders a date before a time later that day, whatever the viewer's time zone", () => {
    vi.stubEnv("TZ", "Asia/Tokyo");
    try {
      expect(
        ["2024-01-01 05:00", "2024-01-01", "2024-01-01T04:00:00+09:00"].sort((a, b) =>
          compareCells(a, b, "date"),
        ),
      ).toEqual(["2024-01-01T04:00:00+09:00", "2024-01-01", "2024-01-01 05:00"]);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("treats two numbers too large to represent as equal", () => {
    expect(compareCells("1e999", "1e999", "number")).toBe(0);
    expect(compareCells("1e999", "-1e999", "number")).toBeGreaterThan(0);
    expect(compareCells("5", "1e999", "number")).toBeLessThan(0);
  });
});
