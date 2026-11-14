import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { DATA_TABLE_MAX_CHARS, DATA_TABLE_MAX_COLUMNS } from "./dataTable";
import { excelSerialToText, isDateFormatCode, readWorkbook, WorkbookError } from "./xlsx";

/** A fixture's bytes, copied into this realm's buffer as the browser's base64 decode makes them. */
function fixture(name: string): Uint8Array {
  const file = readFileSync(`src/shell/__fixtures__/${name}`);
  const bytes = new Uint8Array(file.byteLength);
  bytes.set(file);
  return bytes;
}

describe("readWorkbook", () => {
  // Written by openpyxl: dates, booleans, a blank cell, an uncomputed formula,
  // a second sheet with text, an empty third sheet, and a hidden fourth sheet.
  it("reads every visible sheet in order", async () => {
    const sheets = await readWorkbook(fixture("sales.xlsx"));
    expect(sheets.map((sheet) => sheet.name)).toEqual(["Sales", "Notes", "Empty"]);
    expect(sheets[2].table).toBeNull();
  });

  it("shows numbers, dates, booleans and formulas as Excel would", async () => {
    const [sales] = await readWorkbook(fixture("sales.xlsx"));
    expect(sales.table?.header).toEqual([
      "region",
      "units",
      "revenue",
      "closed",
      "won",
      "column 6",
    ]);
    expect(sales.table?.rows).toEqual([
      ["West", "10", "1234.5", "2026-03-01", "TRUE", "=B2*2"],
      ["East", "2", "99.95", "2026-01-15 09:30:00", "FALSE", ""],
      ["North", "", "0.5", "", "TRUE", ""],
    ]);
    expect(sales.table?.columnTypes).toEqual(["text", "number", "number", "date", "text", "text"]);
    expect(sales.table?.totalRows).toBe(3);
    expect(sales.table?.description).toBe("Excel workbook");
  });

  it("keeps shared text with commas, accents and line breaks", async () => {
    const [, notes] = await readWorkbook(fixture("sales.xlsx"));
    expect(notes.table?.rows).toEqual([["café, crème"], ["line one\nline two"]]);
  });

  it("reads a workbook whose XML carries a namespace prefix", async () => {
    // As the Open XML SDK writes it: <x:workbook>, <x:row>, <x:c>.
    const [report] = await readWorkbook(fixture("prefixed.xlsx"));
    expect(report.name).toBe("Report");
    expect(report.table?.header).toEqual(["item", "count"]);
    expect(report.table?.rows).toEqual([["pens", "12"]]);
  });

  it("shows the first 500 columns of a wider sheet, counting columns and rows past them", async () => {
    const [wide] = await readWorkbook(fixture("wide520.xlsx"));
    expect(wide.table?.header).toHaveLength(DATA_TABLE_MAX_COLUMNS);
    expect(wide.table?.totalColumns).toBe(520);
    expect(wide.table?.totalRows).toBe(2);
  });

  it("calls a sheet with malformed XML damaged, rather than empty", async () => {
    await expect(readWorkbook(fixture("damaged.xlsx"))).rejects.toThrow(
      new WorkbookError("This Excel workbook is damaged."),
    );
  });

  it("calls a zip whose entry points outside the file damaged", async () => {
    const bytes = fixture("sales.xlsx");
    const view = new DataView(bytes.buffer);
    // Walk the central directory to the workbook part and aim its data past the end.
    const end = bytes.length - 22;
    let cursor = view.getUint32(end + 16, true);
    for (let entry = 0; entry < view.getUint16(end + 10, true); entry++) {
      const nameLength = view.getUint16(cursor + 28, true);
      const name = new TextDecoder().decode(bytes.subarray(cursor + 46, cursor + 46 + nameLength));
      if (name === "xl/workbook.xml") view.setUint32(cursor + 42, bytes.length + 1000, true);
      cursor +=
        46 + nameLength + view.getUint16(cursor + 30, true) + view.getUint16(cursor + 32, true);
    }
    await expect(readWorkbook(bytes)).rejects.toThrow(
      new WorkbookError("This Excel workbook is damaged."),
    );
  });

  it("keeps a shared string repeated down the sheet within the text budget", async () => {
    // 1,000 rows all reference one 32,767-character shared string: 33M characters.
    const [sheet] = await readWorkbook(fixture("shared-repeated.xlsx"));
    const rows = sheet.table?.rows ?? [];
    const kept = rows.reduce((total, [cell]) => total + cell.length, 0);
    expect(rows).toHaveLength(Math.floor((DATA_TABLE_MAX_CHARS - "note".length) / 32_767));
    expect(kept).toBeLessThanOrEqual(DATA_TABLE_MAX_CHARS);
    expect(sheet.table?.totalRows).toBe(1000);
  });

  it("says when the browser can't unzip a workbook", async () => {
    vi.stubGlobal("DecompressionStream", undefined);
    try {
      await expect(readWorkbook(fixture("sales.xlsx"))).rejects.toThrow(
        new WorkbookError(
          "This browser can't unzip Excel workbooks. Download the file, or open it in a newer browser.",
        ),
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("explains a file that isn't a workbook", async () => {
    await expect(readWorkbook(new TextEncoder().encode("not a zip"))).rejects.toThrow(
      WorkbookError,
    );
  });
});

describe("excelSerialToText", () => {
  it.each([
    [45658, false, "2025-01-01"],
    [45658.5, false, "2025-01-01 12:00:00"],
    [0.25, false, "06:00:00"],
    // Before Excel's phantom 1900-02-29 (serial 60), serials start a day later.
    [1, false, "1900-01-01"],
    [59, false, "1900-02-28"],
    [60, false, "1900-02-29"],
    [60.5, false, "1900-02-29 12:00:00"],
    [61, false, "1900-03-01"],
    [0, true, "1904-01-01"],
    // Past 9999-12-31 or before the epoch, Excel shows no date; the number stays.
    [1e12, false, "1000000000000"],
    [-1, false, "-1"],
    [2958465, false, "9999-12-31"],
    // The 1904 system reaches 9999 four years of serials sooner.
    [2958465, true, "2958465"],
    // The last millisecond of 9999 is still a date; rounding past it isn't.
    [2958465.99999999, false, "9999-12-31 23:59:59"],
    [2958465.999999999, false, "2958466"],
  ])("%d (1904: %s) → %s", (serial, date1904, expected) => {
    expect(excelSerialToText(serial, date1904)).toBe(expected);
  });
});

describe("isDateFormatCode", () => {
  it.each([
    ["yyyy-mm-dd", true],
    ["[$-409]mmm d, yyyy", true],
    ["[h]:mm", true],
    ["0.00%", false],
    ['"m"0', false],
    ["[Red]#,##0", false],
    ["General", false],
  ])("%s → %s", (code, expected) => {
    expect(isDateFormatCode(code)).toBe(expected);
  });
});
