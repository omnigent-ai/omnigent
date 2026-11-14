import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DATA_TABLE_MAX_CHARS, DATA_TABLE_MAX_COLUMNS, DATA_TABLE_MAX_ROWS } from "./dataTable";
import { ParquetError, parquetCellText, readParquet, SKIPPED_NESTED_VALUE } from "./parquet";

/** A fixture's bytes, copied into this realm's buffer as the browser's base64 decode makes them. */
function fixture(name: string): Uint8Array {
  const file = readFileSync(`src/shell/__fixtures__/${name}`);
  const bytes = new Uint8Array(file.byteLength);
  bytes.set(file);
  return bytes;
}

describe("readParquet", () => {
  // Written by pyarrow with Snappy, its default: ints, floats, a date, a
  // timestamp, booleans, a list column and nulls.
  it("reads the schema and rows, showing each type as text", async () => {
    const table = await readParquet(fixture("sales.snappy.parquet"));
    expect(table?.header).toEqual([
      "region",
      "units",
      "revenue",
      "closed",
      "seen_at",
      "won",
      "tags",
    ]);
    expect(table?.rows).toEqual([
      ["West", "10", "1234.5", "2026-03-01", "2026-03-01T12:00:00Z", "true", '["a","b"]'],
      ["East", "2", "99.95", "2026-01-15", "", "false", "[]"],
      ["North", "", "0.5", "", "2026-02-01T08:05:00Z", "true", ""],
    ]);
    expect(table?.columnTypes.slice(0, 5)).toEqual(["text", "number", "number", "date", "date"]);
    expect(table?.description).toBe("Parquet");
  });

  it("keeps the first rows and counts them all from the footer", async () => {
    const table = await readParquet(fixture("rows2500.parquet"));
    expect(table?.rows).toHaveLength(DATA_TABLE_MAX_ROWS);
    expect(table?.rows.at(-1)).toEqual(["999", "row 999"]);
    expect(table?.totalRows).toBe(2500);
  });

  it("shows the first 500 columns of a wider file, counting them all", async () => {
    const table = await readParquet(fixture("wide520.parquet"));
    expect(table?.header).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(table?.header.at(-1)).toBe("c499");
    expect(table?.totalColumns).toBe(520);
    expect(table?.rows[0].at(-1)).toBe("499");
  });

  // 1,250 bytes: 1,000 rows of a list<string> repeating one 10,000-character
  // dictionary value 2,000 times, which spelled out is 20 billion characters.
  it("builds only as much nested text as the table can keep", async () => {
    const table = await readParquet(fixture("repeated-list.parquet"));

    const kept = [table?.header ?? [], ...(table?.rows ?? [])].flat().join("").length;
    expect(kept).toBeLessThanOrEqual(DATA_TABLE_MAX_CHARS);
    expect(table?.rows).toHaveLength(1);
    // One row, its text cut to what the header left of the budget, and marked cut.
    expect(table?.rows[0]?.[0]).toHaveLength(
      DATA_TABLE_MAX_CHARS - (table?.header[0]?.length ?? 0),
    );
    expect(table?.rows[0]?.[0]?.endsWith("…")).toBe(true);
    expect(table?.textCut).toBe(true);
    expect(table?.totalRows).toBe(1000);
  });

  it("counts only the first rows of a flat column against the value limit", async () => {
    // 3 flat columns × 20,000 rows in one row group: 3,000 values are decoded.
    const table = await readParquet(fixture("flat-one-group.parquet"), 10_000);
    expect(table?.rows).toHaveLength(DATA_TABLE_MAX_ROWS);
    expect(table?.totalRows).toBe(20_000);
  });

  it("skips nested columns packing more values than the preview decodes", async () => {
    // An id column and a list column of 2,000 values a row, over 1,000 rows.
    const table = await readParquet(fixture("list-and-id.parquet"), 1_000_000);
    expect(table?.header).toEqual(["id", "l"]);
    expect(table?.rows).toHaveLength(DATA_TABLE_MAX_ROWS);
    // The flat column still fills in.
    expect(table?.rows.slice(0, 3)).toEqual([
      ["0", SKIPPED_NESTED_VALUE],
      ["1", SKIPPED_NESTED_VALUE],
      ["2", SKIPPED_NESTED_VALUE],
    ]);
  });

  it("names a compression it can't read instead of failing", async () => {
    await expect(readParquet(fixture("sales.zstd.parquet"))).rejects.toThrow(
      new ParquetError(
        "This file is compressed with ZSTD, which the preview can't read yet. Download it to open it.",
      ),
    );
  });

  it.each([
    [new Date(Number.NaN), false, "(out-of-range date)"],
    [new Date("2026-03-01T00:00:00Z"), true, "2026-03-01"],
    [new Date("2026-03-01T12:30:00Z"), false, "2026-03-01T12:30:00Z"],
    [12n, false, "12"],
    [{ a: 1n }, false, '{"a":"1"}'],
    [
      { a: [1, null, "x"], b: { c: true }, skipped: undefined },
      false,
      '{"a":[1,null,"x"],"b":{"c":true}}',
    ],
    [
      [new Date("2026-03-01T00:00:00Z"), new Uint8Array(3)],
      false,
      '["2026-03-01T00:00:00.000Z","(3 bytes)"]',
    ],
  ])("shows %s as %s", (value, isDate, expected) => {
    expect(parquetCellText(value, isDate)).toBe(expected);
  });

  it("stops building a nested value's text at its limit", () => {
    expect(parquetCellText(["abc", "abc", "abc"], false, 8)).toBe('["abc","…');
  });

  it("explains a file that isn't Parquet", async () => {
    await expect(readParquet(new TextEncoder().encode("not parquet at all"))).rejects.toThrow(
      "This file isn't a readable Parquet file.",
    );
  });
});
