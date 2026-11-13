// Parsing for the read-only table preview of delimited data files (.csv, .tsv).

/** Rows the preview keeps; the rest are only counted. */
export const DATA_TABLE_MAX_ROWS = 1000;
/** Characters of cell text the preview keeps, so wide rows can't exhaust memory. */
export const DATA_TABLE_MAX_CHARS = 2_000_000;

const DELIMITED_EXTENSIONS = new Set(["csv", "tsv"]);
const CANDIDATE_DELIMITERS = [",", ";", "\t", "|"] as const;
const SNIFF_RECORDS = 20;

export type DataColumnType = "number" | "date" | "text";

export interface DataTable {
  header: string[];
  rows: string[][];
  /** Data rows in the content, kept or not. */
  totalRows: number;
  columnTypes: DataColumnType[];
  delimiter: string;
  /** How the bytes were decoded, e.g. "utf-8" or "windows-1252". */
  encoding: string;
}

/** Whether *path* opens in the table preview. */
export function isDataTablePath(path: string): boolean {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 && DELIMITED_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
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
  const binary = atob(content);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
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
 * ends keeping, so the kept records are always a prefix. The first record is
 * kept whatever its size.
 */
function parseRecords(
  text: string,
  delimiter: string,
  {
    keepRecords,
    keepChars = Number.POSITIVE_INFINITY,
    scanRecords = Number.POSITIVE_INFINITY,
  }: { keepRecords: number; keepChars?: number; scanRecords?: number },
): { records: string[][]; count: number } {
  const records: string[][] = [];
  let count = 0;
  let keptChars = 0;
  // Past the kept records, fields are only counted, never built.
  let keeping = keepRecords > 0;
  let fields: string[] = [];
  let field = "";
  let fieldHasChars = false;
  let inQuotes = false;
  let recordHasContent = false;

  const endRecord = () => {
    // A blank line, or the newline that ends the file, is not a record.
    if (recordHasContent || fieldHasChars) {
      count += 1;
      if (keeping) {
        fields.push(field);
        const size = fields.reduce((sum, value) => sum + value.length, 0);
        if (records.length === 0 || keptChars + size <= keepChars) {
          records.push(fields);
          keptChars += size;
          keeping = records.length < keepRecords;
        } else {
          keeping = false;
        }
      }
    }
    fields = [];
    field = "";
    fieldHasChars = false;
    recordHasContent = false;
  };

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          fieldHasChars = true;
          if (keeping) field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        fieldHasChars = true;
        if (keeping) field += char;
      }
      continue;
    }
    if (char === '"' && !fieldHasChars) {
      inQuotes = true;
      recordHasContent = true;
    } else if (char === delimiter) {
      if (keeping) fields.push(field);
      field = "";
      fieldHasChars = false;
      recordHasContent = true;
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      endRecord();
      if (count >= scanRecords) break;
    } else {
      fieldHasChars = true;
      if (keeping) field += char;
    }
  }
  if ((fieldHasChars || recordHasContent) && count < scanRecords) endRecord();
  return { records, count };
}

const NUMBER_PATTERN = /^[-+]?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?([eE][-+]?\d+)?%?$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

/** The number a cell holds, or ``NaN`` when it holds none. */
export function cellNumber(value: string): number {
  const trimmed = value.trim();
  if (!trimmed || !/\d/.test(trimmed) || !NUMBER_PATTERN.test(trimmed)) return Number.NaN;
  return Number.parseFloat(trimmed.replace(/,/g, "").replace(/%$/, ""));
}

function isDateCell(value: string): boolean {
  const trimmed = value.trim();
  return DATE_PATTERN.test(trimmed) && !Number.isNaN(Date.parse(trimmed));
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
export function compareCells(a: string, b: string, type: DataColumnType): number {
  const aEmpty = a.trim() === "";
  const bEmpty = b.trim() === "";
  if (aEmpty || bEmpty) return Number(aEmpty) - Number(bEmpty);
  if (type === "number") return cellNumber(a) - cellNumber(b);
  if (type === "date") return Date.parse(a.trim()) - Date.parse(b.trim());
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

/**
 * Parse a delimited file for the table preview.
 *
 * The first record is the header. Short rows are padded and long ones keep
 * their extra cells under generated column names.
 *
 * @param text - The decoded file text.
 * @param path - The file's path, which picks the delimiter for ``.tsv``.
 * @param encoding - How *text* was decoded, reported back unchanged.
 * @returns The table, or ``null`` when the file has no header row.
 */
export function parseDataTable(text: string, path: string, encoding = "utf-8"): DataTable | null {
  const delimiter = detectDelimiter(text, path);
  const { records, count } = parseRecords(text, delimiter, {
    keepRecords: DATA_TABLE_MAX_ROWS + 1,
    keepChars: DATA_TABLE_MAX_CHARS,
  });
  if (records.length === 0) return null;
  const [header, ...rows] = records;
  const width = Math.max(header.length, ...rows.map((row) => row.length));
  const columns = Array.from(
    { length: width },
    (_, index) => header[index] ?? `column ${index + 1}`,
  );
  const padded = rows.map((row) =>
    row.length === width ? row : [...row, ...Array<string>(width - row.length).fill("")],
  );
  return {
    header: columns,
    rows: padded,
    totalRows: count - 1,
    columnTypes: columns.map((_, index) => inferColumnType(padded.map((row) => row[index]))),
    delimiter,
    encoding,
  };
}
