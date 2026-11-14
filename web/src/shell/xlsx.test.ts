import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { DATA_TABLE_MAX_CHARS, DATA_TABLE_MAX_COLUMNS } from "./dataTable";
import {
  elapsedFormat,
  elapsedText,
  excelSerialToText,
  isDateFormatCode,
  isElapsedFormatCode,
  isTimeFormatCode,
  readWorkbook,
  resolvePartPath,
  WorkbookError,
} from "./xlsx";

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

describe("readWorkbook time formats and limits", () => {
  // Hand-built: a built-in time format, a built-in date format and a custom
  // "hh:mm" format, each holding a serial, and a formula with its saved result.
  it.each([
    ["times1900.xlsx", ["00:00:00", "1899-12-31", "12:00:00", "3", "36:00:00", "2160:00"]],
    ["times1904.xlsx", ["00:00:00", "1904-01-01", "12:00:00", "3", "36:00:00", "2160:00"]],
  ])("shows %s's times, durations and a saved formula result as Excel does", async (name, row) => {
    const [sheet] = await readWorkbook(fixture(name));
    expect(sheet.table?.header).toEqual([
      "time",
      "date",
      "short time",
      "total",
      "elapsed",
      "minutes",
    ]);
    expect(sheet.table?.rows).toEqual([row]);
  });

  it("marks a sheet that inflates past the limit whatever size it declares", async () => {
    // Two sheets share a part whose headers say 1 KiB; it inflates past 64 MiB.
    const sheets = await readWorkbook(fixture("understated.xlsx"));
    expect(sheets.map((sheet) => [sheet.name, sheet.table, sheet.message])).toEqual([
      ["Times", null, "This sheet is too large to preview. Download the workbook to open it."],
      ["Again", null, "This sheet is too large to preview. Download the workbook to open it."],
    ]);
  });

  // Small and Again point at one part; Big is stored uncompressed, 20 KB.
  it("reads a shared part once and marks only the sheets past the workbook limit", async () => {
    const sheets = await readWorkbook(fixture("budget.xlsx"), 10_000);
    expect(sheets.map((sheet) => [sheet.name, sheet.table?.rows, sheet.message])).toEqual([
      ["Small", [["pens"]], undefined],
      ["Big", undefined, "This sheet is too large to preview. Download the workbook to open it."],
      ["Again", [["pens"]], undefined],
    ]);
    expect(sheets[2].table).toBe(sheets[0].table);
  });

  it("counts every part read against the workbook limit, not just the largest", async () => {
    // Big (20,140 bytes) fits the 20,500-byte limit alone, but not after the
    // package's other parts (about 1,300 bytes) were read.
    const sheets = await readWorkbook(fixture("budget.xlsx"), 20_500);
    expect(sheets.map((sheet) => [sheet.name, sheet.message])).toEqual([
      ["Small", undefined],
      ["Big", "This sheet is too large to preview. Download the workbook to open it."],
      ["Again", undefined],
    ]);
  });
});

describe("workbook relationships", () => {
  // Hand-built: the same sheet (shared-string header and row, a date-styled
  // serial) with one part moved where only its relationship says.
  it.each(["renamed-strings.xlsx", "renamed-styles.xlsx", "parent-relative-sheet.xlsx"])(
    "resolves workbook relationship targets for %s",
    async (name) => {
      const [sheet] = await readWorkbook(fixture(name));
      expect(sheet.name).toBe("Orders");
      expect(sheet.message).toBeUndefined();
      expect(sheet.table?.header).toEqual(["item", "when"]);
      expect(sheet.table?.rows).toEqual([["pens", "2026-03-01"]]);
      expect(sheet.table?.totalRows).toBe(1);
    },
  );

  it("finds a relocated workbook through the package and skips an external sheet", async () => {
    // The workbook is book/main.xml; its sheet part's name is percent-encoded
    // in the relationship; a second sheet points outside the package.
    const sheets = await readWorkbook(fixture("relocated-workbook.xlsx"));
    expect(sheets.map((sheet) => [sheet.name, sheet.table?.rows])).toEqual([
      ["Orders", [["pens", "2026-03-01"]]],
      ["Linked", undefined],
    ]);
  });

  it.each([
    ["xl/workbook.xml", "worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"],
    ["xl/workbook.xml", "../xl/worksheets/./sheet1.xml", "xl/worksheets/sheet1.xml"],
    ["xl/workbook.xml", "/xl/styles.xml", "xl/styles.xml"],
    ["", "xl/workbook.xml", "xl/workbook.xml"],
    ["xl/workbook.xml", "sheet%201.xml", "xl/sheet%201.xml"],
    ["xl/workbook.xml", "../../outside.xml", null],
  ])("resolves %s → %s as %s", (source, target, expected) => {
    expect(resolvePartPath(source, target)).toBe(expected);
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
    // Times round to the nearest second, as Excel shows them.
    [0.4166666, false, "10:00:00"],
    // The day is the rounded time's, on either side of the phantom leap day.
    [59.999999, false, "1900-02-29 00:00:00"],
    [60.999999, false, "1900-03-01 00:00:00"],
    // The last second of 9999 is still a date; a time rounding past it isn't.
    [2958465.99998, false, "9999-12-31 23:59:58"],
    [2958465.99999999, false, "2958465.99999999"],
  ])("%d (1904: %s) → %s", (serial, date1904, expected) => {
    expect(excelSerialToText(serial, date1904)).toBe(expected);
  });
});

describe("isTimeFormatCode", () => {
  it.each([
    ["hh:mm", true],
    ["h:mm AM/PM", true],
    ["[h]:mm:ss", false],
    ["mm:ss", true],
    ["e h:mm", false],
    ["yyyy-mm-dd hh:mm", false],
    ["mmm-yy", false],
    ["d-mmm", false],
  ])("%s → %s", (code, expected) => {
    expect(isTimeFormatCode(code)).toBe(expected);
  });
});

describe("elapsed time", () => {
  it.each([
    ["[h]:mm:ss", { unit: "h", seconds: true }],
    ["[h]:mm", { unit: "h", seconds: false }],
    ["[mm]:ss", { unit: "m", seconds: true }],
    ["[ss]", { unit: "s", seconds: true }],
    ["h:mm", null],
    ['"[h]" mm', null],
  ])("%s → %j", (code, expected) => {
    expect(elapsedFormat(code)).toEqual(expected);
    expect(isElapsedFormatCode(code)).toBe(expected !== null);
  });

  it.each([
    [1.5, { unit: "h", seconds: true }, "36:00:00"],
    [1.5, { unit: "h", seconds: false }, "36:00"],
    [1.5, { unit: "m", seconds: true }, "2160:00"],
    [1.5, { unit: "s", seconds: true }, "129600"],
    [0.0006944444, { unit: "h", seconds: true }, "0:01:00"],
    [-0.5, { unit: "h", seconds: true }, "-12:00:00"],
  ] as const)("%d days as %j → %s", (days, format, expected) => {
    expect(elapsedText(days, format)).toBe(expected);
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
