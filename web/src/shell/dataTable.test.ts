import { describe, expect, it } from "vitest";
import {
  DATA_TABLE_MAX_CHARS,
  DATA_TABLE_MAX_ROWS,
  compareCells,
  decodeDataFile,
  detectDelimiter,
  inferColumnType,
  isDataTablePath,
  parseDataTable,
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

  it("returns null for an empty file", () => {
    expect(parseDataTable("", "empty.csv")).toBeNull();
    expect(parseDataTable("\n\n", "empty.csv")).toBeNull();
  });

  it("keeps the first rows and counts every row", () => {
    const rows = Array.from({ length: DATA_TABLE_MAX_ROWS + 500 }, (_, i) => `${i},x`);
    const table = parseDataTable(["id,value", ...rows].join("\n"), "big.csv");
    expect(table?.rows).toHaveLength(DATA_TABLE_MAX_ROWS);
    expect(table?.rows.at(-1)).toEqual([String(DATA_TABLE_MAX_ROWS - 1), "x"]);
    expect(table?.totalRows).toBe(DATA_TABLE_MAX_ROWS + 500);
  });

  it("stops keeping rows at the first one past the character budget", () => {
    const wide = "x".repeat(DATA_TABLE_MAX_CHARS);
    // The wide row doesn't fit; the short row after it isn't kept either, so the
    // kept rows stay a prefix of the file.
    const table = parseDataTable(`col\nshort\n${wide}\nlater\n`, "wide.csv");
    expect(table?.rows).toEqual([["short"]]);
    expect(table?.totalRows).toBe(3);
  });

  it("keeps an oversized header as the header", () => {
    const wideHeader = "h".repeat(DATA_TABLE_MAX_CHARS + 1);
    const table = parseDataTable(`${wideHeader},b\n1,2\n`, "wide.csv");
    expect(table?.header).toEqual([wideHeader, "b"]);
    expect(table?.totalRows).toBe(1);
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

  it("decodes UTF-16 marked by its byte-order mark", () => {
    const bytes = [0xff, 0xfe, 0x61, 0x00, 0x2c, 0x00, 0x62, 0x00];
    expect(decodeDataFile(base64Of(bytes), "base64")).toEqual({
      text: "a,b",
      encoding: "utf-16le",
    });
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
    expect(["item10", "item2"].sort((a, b) => compareCells(a, b, "text"))).toEqual([
      "item2",
      "item10",
    ]);
  });
});
