// Parsing for the read-only table preview of data files: delimited text
// (.csv, .tsv) here, Excel and Parquet in xlsx.ts and parquet.ts.

/** Rows the preview keeps; the rest are only counted. */
export const DATA_TABLE_MAX_ROWS = 1000;
/** Characters of cell text the preview keeps, so wide rows can't exhaust memory. */
export const DATA_TABLE_MAX_CHARS = 2_000_000;
/** Longest a column name is kept; 500 of them stay well within the text budget. */
export const DATA_TABLE_MAX_NAME_CHARS = 1_000;
/** Columns the preview keeps; a row of delimiters can't widen every row past this. */
export const DATA_TABLE_MAX_COLUMNS = 500;
/** Cells the preview keeps, header and padding included, so a wide table keeps fewer rows. */
export const DATA_TABLE_MAX_CELLS = 200_000;

const DELIMITED_EXTENSIONS = new Set(["csv", "tsv"]);
/** Binary formats read in the browser; they always open on the table. */
const SPREADSHEET_EXTENSIONS = new Set(["xlsx", "parquet"]);
const DELIMITER_LABELS: Record<string, string> = {
  ",": "comma-separated",
  ";": "semicolon-separated",
  "\t": "tab-separated",
  "|": "pipe-separated",
};
const CANDIDATE_DELIMITERS = [",", ";", "\t", "|"] as const;
const SNIFF_RECORDS = 20;

export type DataColumnType = "number" | "date" | "text";

export interface DataTable {
  header: string[];
  rows: string[][];
  /** Data rows in the content, kept or not. */
  totalRows: number;
  /** Fields in the content's widest row, kept or not. */
  totalColumns: number;
  columnTypes: DataColumnType[];
  /** Where the table came from, e.g. "comma-separated · windows-1252" or "Parquet". */
  description: string;
  /** Whether text past the budget was cut from a column name or the first row. */
  textCut?: boolean;
}

function extensionOf(path: string): string | null {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : null;
}

/** Whether *path* is delimited text that opens in the table preview, with a source view. */
export function isDataTablePath(path: string): boolean {
  return DELIMITED_EXTENSIONS.has(extensionOf(path) ?? "");
}

/** Whether *path* is an Excel workbook or Parquet file, which open only as a table. */
export function isSpreadsheetPath(path: string): boolean {
  return SPREADSHEET_EXTENSIONS.has(extensionOf(path) ?? "");
}

/** The bytes of base64 *content*, as the server sends a binary file. */
export function decodeBase64(content: string): Uint8Array {
  const binary = atob(content);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/**
 * Decode a data file's content as the server sent it.
 *
 * The server sends UTF-8 text as is and anything else as base64, so a
 * Latin-1 export arrives as base64. A byte-order mark picks UTF-16; otherwise
 * strict UTF-8 is tried before falling back to Windows-1252.
 *
 * @param content - The file content, e.g. ``"a,b\n1,2"``.
 * @param encoding - How the server encoded it: ``"utf-8"`` or ``"base64"``.
 * @returns The text, without a byte-order mark, and the encoding used.
 */
export function decodeDataFile(
  content: string,
  encoding: "utf-8" | "base64",
): { text: string; encoding: string } {
  if (encoding !== "base64") return { text: stripBom(content), encoding: "utf-8" };
  const bytes = decodeBase64(content);
  if (bytes[0] === 0xff && bytes[1] === 0xfe) {
    return { text: new TextDecoder("utf-16le").decode(bytes.subarray(2)), encoding: "utf-16le" };
  }
  if (bytes[0] === 0xfe && bytes[1] === 0xff) {
    return { text: new TextDecoder("utf-16be").decode(bytes.subarray(2)), encoding: "utf-16be" };
  }
  try {
    return {
      text: stripBom(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
      encoding: "utf-8",
    };
  } catch {
    return { text: new TextDecoder("windows-1252").decode(bytes), encoding: "windows-1252" };
  }
}

function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/**
 * Pick the delimiter of a delimited file.
 *
 * A ``.tsv`` file is tab-separated. Otherwise each candidate splits the first
 * records; one wins when at least half of them share a field count above one,
 * preferring the most agreement, then the most fields. A single-column file
 * falls back to a comma.
 *
 * @param text - The decoded file text.
 * @param path - The file's path, e.g. ``"out/results.csv"``.
 * @returns The delimiter, e.g. ``","``.
 */
export function detectDelimiter(text: string, path: string): string {
  if (path.toLowerCase().endsWith(".tsv")) return "\t";
  let best = ",";
  let bestAgreement = 0;
  let bestFields = 1;
  for (const delimiter of CANDIDATE_DELIMITERS) {
    const { records } = parseRecords(text, delimiter, {
      keepRecords: SNIFF_RECORDS,
      scanRecords: SNIFF_RECORDS,
      keepFields: DATA_TABLE_MAX_COLUMNS,
      keepChars: DATA_TABLE_MAX_CHARS,
    });
    const tally = new Map<number, number>();
    for (const fields of records) tally.set(fields.length, (tally.get(fields.length) ?? 0) + 1);
    let fieldCount = 1;
    let agreement = 0;
    for (const [count, agreeing] of tally) {
      if (agreeing > agreement || (agreeing === agreement && count > fieldCount)) {
        fieldCount = count;
        agreement = agreeing;
      }
    }
    if (fieldCount < 2 || agreement * 2 < records.length) continue;
    if (agreement > bestAgreement || (agreement === bestAgreement && fieldCount > bestFields)) {
      best = delimiter;
      bestAgreement = agreement;
      bestFields = fieldCount;
    }
  }
  return best;
}

/**
 * Split delimited text into records (RFC 4180: quoted fields may hold the
 * delimiter, newlines and doubled quotes).
 *
 * Records are counted up to *scanRecords*. The first *keepRecords* are kept
 * while their field text fits in *keepChars*; the first record that doesn't
 * ends keeping, so the kept records are always a prefix. The first two records
 * (the header and the first row) are kept even with their text cut at
 * *keepChars*, which *textCut* reports. Each kept record keeps at most
 * *keepFields* fields; *widest* counts the fields of every record, kept or not.
 */
function parseRecords(
  text: string,
  delimiter: string,
  {
    keepRecords,
    keepChars = Number.POSITIVE_INFINITY,
    scanRecords = Number.POSITIVE_INFINITY,
    keepFields = Number.POSITIVE_INFINITY,
  }: { keepRecords: number; keepChars?: number; scanRecords?: number; keepFields?: number },
): { records: string[][]; count: number; widest: number; textCut: boolean } {
  const records: string[][] = [];
  let textCut = false;
  let count = 0;
  let widest = 0;
  let fieldCount = 0;
  let keptChars = 0;
  // Text of the current record's kept fields, and whether some was left out for the budget.
  let recordChars = 0;
  let recordOverflowed = false;
  // Past the kept records, fields are only counted, never built.
  let keeping = keepRecords > 0;
  let fields: string[] = [];
  let field = "";
  let fieldHasChars = false;
  let inQuotes = false;
  let recordHasContent = false;
  // Whether the field being read is kept: past *keepFields*, it is only counted.
  const keepingField = () => keeping && fields.length < keepFields;
  // Append to the field being read while the record's text fits what's left of the budget.
  const append = (chars: string) => {
    if (!keepingField()) return;
    let kept = chars;
    // A header name is kept only up to the name limit, so its excess costs nothing.
    if (records.length === 0 && field.length + kept.length > DATA_TABLE_MAX_NAME_CHARS) {
      kept = kept.slice(0, Math.max(0, DATA_TABLE_MAX_NAME_CHARS - field.length));
      textCut = true;
      if (kept === "") return;
    }
    if (recordChars + field.length + kept.length > keepChars - keptChars) {
      recordOverflowed = true;
      return;
    }
    field += kept;
  };

  const endRecord = () => {
    // A blank line, or the newline that ends the file, is not a record.
    if (recordHasContent || fieldHasChars) {
      count += 1;
      widest = Math.max(widest, fieldCount + 1);
      if (keeping) {
        if (fields.length < keepFields) fields.push(field);
        // The header and the first data row are kept even cut short, as the
        // other readers keep them; a later record that overflows ends keeping.
        const keptSome = recordChars + field.length > 0;
        if (records.length === 0 || !recordOverflowed || (records.length === 1 && keptSome)) {
          textCut ||= recordOverflowed;
          records.push(fields);
          keptChars += recordChars + field.length;
          keeping = records.length < keepRecords;
        } else {
          keeping = false;
        }
      }
    }
    fields = [];
    field = "";
    fieldCount = 0;
    recordChars = 0;
    recordOverflowed = false;
    fieldHasChars = false;
    recordHasContent = false;
  };

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          fieldHasChars = true;
          append('"');
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        fieldHasChars = true;
        append(char);
      }
      continue;
    }
    if (char === '"' && !fieldHasChars) {
      inQuotes = true;
      recordHasContent = true;
    } else if (char === delimiter) {
      if (keepingField()) {
        fields.push(field);
        recordChars += field.length;
      }
      fieldCount += 1;
      field = "";
      fieldHasChars = false;
      recordHasContent = true;
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      endRecord();
      if (count >= scanRecords) break;
    } else {
      fieldHasChars = true;
      append(char);
    }
  }
  if ((fieldHasChars || recordHasContent) && count < scanRecords) endRecord();
  return { records, count, widest, textCut };
}

const NUMBER_PATTERN = /^[-+]?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?([eE][-+]?\d+)?%?$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

/** The number a cell holds, ``"25%"`` as 0.25, or ``NaN`` when it holds none. */
export function cellNumber(value: string): number {
  const trimmed = value.trim();
  if (!trimmed || !/\d/.test(trimmed) || !NUMBER_PATTERN.test(trimmed)) return Number.NaN;
  const number = Number.parseFloat(trimmed.replace(/,/g, "").replace(/%$/, ""));
  return trimmed.endsWith("%") ? number / 100 : number;
}

/**
 * A date cell's time in milliseconds. A date or time without an offset is read
 * as UTC, so a date sorts as the midnight starting it whatever the viewer's zone.
 */
function cellDate(value: string): number {
  const trimmed = value.trim();
  if (!DATE_PATTERN.test(trimmed)) return Number.NaN;
  const iso = trimmed.replace(" ", "T");
  if (!iso.includes("T")) return Date.parse(`${iso}T00:00Z`);
  return Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`);
}

function isDateCell(value: string): boolean {
  return !Number.isNaN(cellDate(value));
}

/**
 * Infer a column's type from its non-empty cells: number, date, else text.
 *
 * @param values - The column's cells, e.g. ``["1", "2.5", ""]``.
 * @returns The inferred type, e.g. ``"number"``.
 */
export function inferColumnType(values: string[]): DataColumnType {
  const present = values.filter((value) => value.trim() !== "");
  if (present.length === 0) return "text";
  if (present.every((value) => !Number.isNaN(cellNumber(value)))) return "number";
  if (present.every(isDateCell)) return "date";
  return "text";
}

/**
 * Order two cells of a column of *type*; empty cells sort last.
 *
 * @returns Negative, zero or positive, as for ``Array.prototype.sort``.
 */
// Ordered by comparison rather than subtraction, which gives NaN for two infinities.
function compareValues(a: number, b: number): number {
  return a === b ? 0 : a < b ? -1 : 1;
}

export function compareCells(a: string, b: string, type: DataColumnType): number {
  const aEmpty = a.trim() === "";
  const bEmpty = b.trim() === "";
  if (aEmpty || bEmpty) return Number(aEmpty) - Number(bEmpty);
  if (type === "number") return compareValues(cellNumber(a), cellNumber(b));
  if (type === "date") return compareValues(cellDate(a), cellDate(b));
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function textLength(cells: string[]): number {
  return cells.reduce((total, cell) => total + cell.length, 0);
}

/**
 * *cells* fitted to *budget* characters: whole until one doesn't fit, which is
 * cut and ends in ``…``; the cells after it are left blank.
 */
function cutToBudget(cells: string[], budget: number): string[] {
  let left = budget;
  return cells.map((cell) => {
    if (cell.length <= left) {
      left -= cell.length;
      return cell;
    }
    // Cutting between a surrogate pair would leave half a character.
    const end = /[\uD800-\uDBFF]/.test(cell[left - 2] ?? "") ? left - 2 : left - 1;
    const cut = left > 0 ? `${cell.slice(0, Math.max(end, 0))}…` : "";
    left = 0;
    return cut;
  });
}

/**
 * Build the preview table from records whose first is the header.
 *
 * Keeps at most ``DATA_TABLE_MAX_COLUMNS`` columns, and as many rows as fit in
 * ``DATA_TABLE_MAX_CELLS`` (padding fills every row to the widest one) and in
 * ``DATA_TABLE_MAX_CHARS`` of text. Short rows are padded and long ones keep
 * their extra cells under generated column names.
 *
 * @param records - The header, then at most ``DATA_TABLE_MAX_ROWS`` data rows.
 * @param totalRows - Data rows in the file, kept or not.
 * @param totalColumns - Fields in the file's widest row, kept or not.
 * @param description - Where the table came from, e.g. ``"Parquet"``.
 * @param textCutEarlier - Whether the reader already cut text from *records*.
 * @returns The table, or ``null`` when there is no header row.
 */
export function tableFromRecords(
  records: string[][],
  totalRows: number,
  totalColumns: number,
  description: string,
  textCutEarlier = false,
): DataTable | null {
  if (records.length === 0) return null;
  const [header, ...parsedRows] = records;
  const width = Math.min(
    DATA_TABLE_MAX_COLUMNS,
    Math.max(header.length, ...parsedRows.map((row) => row.length)),
  );
  const rows = parsedRows.slice(0, Math.max(1, Math.floor(DATA_TABLE_MAX_CELLS / width) - 1));
  // One text budget covers the header and the kept rows, so a value repeated in
  // many cells can't multiply past it. The header is cut to fit; rows are kept
  // while they fit, so the kept rows stay a prefix of the file.
  let budget = DATA_TABLE_MAX_CHARS;
  let textCut = textCutEarlier;
  const columns = Array.from({ length: width }, (_, index) => {
    const name = header[index] ?? "";
    const cut = name.slice(0, DATA_TABLE_MAX_NAME_CHARS);
    textCut ||= cut.length < name.length;
    // A blank name still needs a label, which counts against the budget too.
    const label = cut.trim() === "" ? `column ${index + 1}` : cut;
    budget -= label.length;
    return label;
  });
  const kept: string[][] = [];
  for (const row of rows) {
    const cells = row.length > width ? row.slice(0, width) : row;
    const length = textLength(cells);
    if (length > budget) {
      // The first row is kept with its text cut, so an oversized row still shows.
      if (kept.length === 0 && budget > 0) {
        kept.push(cutToBudget(cells, budget));
        textCut = true;
      }
      break;
    }
    budget -= length;
    kept.push(cells);
  }
  const padded = kept.map((row) =>
    row.length === width ? row : [...row, ...Array<string>(width - row.length).fill("")],
  );
  return {
    header: columns,
    rows: padded,
    totalRows,
    totalColumns: Math.max(totalColumns, width),
    columnTypes: columns.map((_, index) => inferColumnType(padded.map((row) => row[index]))),
    description,
    ...(textCut && { textCut }),
  };
}

/**
 * Parse a delimited file for the table preview.
 *
 * @param text - The decoded file text.
 * @param path - The file's path, which picks the delimiter for ``.tsv``.
 * @param encoding - How *text* was decoded, e.g. ``"windows-1252"``; named
 *   in the description when it isn't UTF-8.
 * @returns The table, or ``null`` when the file has no header row.
 */
export function parseDataTable(text: string, path: string, encoding = "utf-8"): DataTable | null {
  const delimiter = detectDelimiter(text, path);
  const { records, count, widest, textCut } = parseRecords(text, delimiter, {
    keepRecords: DATA_TABLE_MAX_ROWS + 1,
    keepChars: DATA_TABLE_MAX_CHARS,
    keepFields: DATA_TABLE_MAX_COLUMNS,
  });
  const description = [DELIMITER_LABELS[delimiter], encoding === "utf-8" ? null : encoding]
    .filter(Boolean)
    .join(" · ");
  return tableFromRecords(records, count - 1, widest, description, textCut);
}
