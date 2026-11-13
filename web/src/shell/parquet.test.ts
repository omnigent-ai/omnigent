import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DATA_TABLE_MAX_COLUMNS, DATA_TABLE_MAX_ROWS } from "./dataTable";
import { ParquetError, parquetCellText, readParquet } from "./parquet";

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
  ])("shows %s as %s", (value, isDate, expected) => {
    expect(parquetCellText(value, isDate)).toBe(expected);
  });

  it("explains a file that isn't Parquet", async () => {
    await expect(readParquet(new TextEncoder().encode("not parquet at all"))).rejects.toThrow(
      "This file isn't a readable Parquet file.",
    );
  });
});
