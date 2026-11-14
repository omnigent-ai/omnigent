// A minimal Excel (.xlsx) reader for the table preview, with no dependency:
// the workbook is a zip of XML parts, unzipped with the browser's
// DecompressionStream and read with DOMParser.

import {
  DATA_TABLE_MAX_COLUMNS,
  DATA_TABLE_MAX_ROWS,
  tableFromRecords,
  type DataTable,
} from "./dataTable";

/** Most bytes one part may inflate to, so a zip bomb can't exhaust memory. */
const MAX_PART_BYTES = 64 * 1024 * 1024;

const END_OF_CENTRAL_DIRECTORY = 0x06054b50;
const CENTRAL_DIRECTORY_ENTRY = 0x02014b50;
const LOCAL_FILE_HEADER = 0x04034b50;

/** Excel's built-in number formats that show a date or time. */
const BUILT_IN_DATE_FORMATS = new Set([
  14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51,
  52, 53, 54, 55, 56, 57, 58,
]);
const DAY_MS = 86_400_000;
// Day zero of each date system, in UTC. Excel's 1900 system counts a 29 February
// 1900 that never was (serial 60): this epoch is right from March 1900 on, and
// earlier serials start a day later.
const EPOCH_1900 = Date.UTC(1899, 11, 30);
const PHANTOM_LEAP_DAY = 60;
const EPOCH_1904 = Date.UTC(1904, 0, 1);
/** The last moment Excel shows as a date: the end of 9999-12-31. */
const LAST_DATE_MS = Date.UTC(10000, 0, 1);

/** A workbook that can't be read, with a message for the person viewing it. */
export class WorkbookError extends Error {}

export interface WorkbookSheet {
  name: string;
  /** ``null`` for a sheet with no cells. */
  table: DataTable | null;
}

interface ZipEntry {
  method: number;
  compressedSize: number;
  uncompressedSize: number;
  offset: number;
}

function zipEntries(bytes: Uint8Array): Map<string, ZipEntry> {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  // The directory's end record sits 22 bytes from the end, before an optional comment.
  let end = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 0xffff); i--) {
    if (view.getUint32(i, true) === END_OF_CENTRAL_DIRECTORY) {
      end = i;
      break;
    }
  }
  if (end < 0) {
    throw new WorkbookError(
      "This file isn't a readable Excel workbook. It may be password-protected or an older .xls file.",
    );
  }
  const entries = new Map<string, ZipEntry>();
  const count = view.getUint16(end + 10, true);
  let cursor = view.getUint32(end + 16, true);
  const names = new TextDecoder();
  for (let n = 0; n < count; n++) {
    if (cursor + 46 > bytes.length || view.getUint32(cursor, true) !== CENTRAL_DIRECTORY_ENTRY) {
      throw new WorkbookError("This Excel workbook is damaged.");
    }
    const nameLength = view.getUint16(cursor + 28, true);
    const name = names.decode(bytes.subarray(cursor + 46, cursor + 46 + nameLength));
    entries.set(name, {
      method: view.getUint16(cursor + 10, true),
      compressedSize: view.getUint32(cursor + 20, true),
      uncompressedSize: view.getUint32(cursor + 24, true),
      offset: view.getUint32(cursor + 42, true),
    });
    cursor +=
      46 + nameLength + view.getUint16(cursor + 30, true) + view.getUint16(cursor + 32, true);
  }
  return entries;
}

/** The browser's raw-deflate decompressor; older browsers have none. */
function rawInflater(): DecompressionStream {
  try {
    return new DecompressionStream("deflate-raw");
  } catch {
    throw new WorkbookError(
      "This browser can't unzip Excel workbooks. Download the file, or open it in a newer browser.",
    );
  }
}

async function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const source = new ReadableStream<BufferSource>({
    start(controller) {
      controller.enqueue(data as Uint8Array<ArrayBuffer>);
      controller.close();
    },
  });
  const reader = source.pipeThrough(rawInflater()).getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  // Sequential by design: each read waits for the previous chunk.
  /* oxlint-disable no-await-in-loop */
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_PART_BYTES) {
      await reader.cancel();
      throw new WorkbookError("This workbook is too large to preview. Download it to open it.");
    }
    chunks.push(value);
  }
  /* oxlint-enable no-await-in-loop */
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

async function readPart(
  bytes: Uint8Array,
  entries: Map<string, ZipEntry>,
  name: string,
): Promise<Document | null> {
  const entry = entries.get(name);
  if (!entry) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(entry.offset, true) !== LOCAL_FILE_HEADER) {
    throw new WorkbookError("This Excel workbook is damaged.");
  }
  if (entry.uncompressedSize > MAX_PART_BYTES) {
    throw new WorkbookError("This workbook is too large to preview. Download it to open it.");
  }
  const start =
    entry.offset +
    30 +
    view.getUint16(entry.offset + 26, true) +
    view.getUint16(entry.offset + 28, true);
  const data = bytes.subarray(start, start + entry.compressedSize);
  let xml: Uint8Array;
  if (entry.method === 0) xml = data;
  else if (entry.method === 8) xml = await inflateRaw(data);
  else throw new WorkbookError("This workbook uses a compression the preview can't read.");
  const parsed = new DOMParser().parseFromString(new TextDecoder().decode(xml), "application/xml");
  // DOMParser reports malformed XML as a <parsererror> element, not by throwing.
  if (parsed.getElementsByTagName("parsererror").length > 0) {
    throw new WorkbookError("This Excel workbook is damaged.");
  }
  return parsed;
}

/**
 * *node*'s descendants named *name*, whatever their namespace prefix: the Open
 * XML SDK writes ``<x:row>`` where Excel writes ``<row>``.
 */
function descendants(node: Document | Element, name: string): Element[] {
  return Array.from(node.getElementsByTagNameNS("*", name));
}

/** The text of *element*'s ``<t>`` runs, leaving out phonetic guides. */
function runText(element: Element): string {
  return descendants(element, "t")
    .filter((run) => run.parentElement?.localName !== "rPh")
    .map((run) => run.textContent ?? "")
    .join("");
}

/** Whether a custom number format shows a date or time, e.g. ``"yyyy-mm-dd"``. */
export function isDateFormatCode(code: string): boolean {
  const bare = code
    .replace(/"[^"]*"/g, "")
    .replace(/\\./g, "")
    .replace(/\[(?!h+\]|m+\]|s+\])[^\]]*\]/gi, "");
  return /[dmyhs]/i.test(bare) && !/general/i.test(bare);
}

function dateStyles(styles: Document | null): Set<number> {
  const dated = new Set<number>();
  if (!styles) return dated;
  const custom = new Map<number, string>();
  for (const format of descendants(styles, "numFmt")) {
    custom.set(Number(format.getAttribute("numFmtId")), format.getAttribute("formatCode") ?? "");
  }
  const cellFormats = descendants(styles, "cellXfs")[0];
  if (!cellFormats) return dated;
  descendants(cellFormats, "xf").forEach((format, index) => {
    const id = Number(format.getAttribute("numFmtId") ?? 0);
    const code = custom.get(id);
    if (code !== undefined ? isDateFormatCode(code) : BUILT_IN_DATE_FORMATS.has(id)) {
      dated.add(index);
    }
  });
  return dated;
}

/**
 * Show an Excel date serial as ISO text: a date, a time, or both.
 *
 * @param serial - Days since the workbook's epoch, e.g. ``46082.5``.
 * @param date1904 - Whether the workbook uses the 1904 date system.
 * @returns E.g. ``"2026-03-01"``, ``"12:00:00"`` or ``"2026-03-01 12:00:00"``.
 */
export function excelSerialToText(serial: number, date1904: boolean): string {
  const epoch = date1904
    ? EPOCH_1904
    : serial < PHANTOM_LEAP_DAY
      ? EPOCH_1900 + DAY_MS
      : EPOCH_1900;
  const ms = epoch + Math.round(serial * DAY_MS);
  // Excel shows no date before its epoch or past 9999; the number stays visible.
  if (!(serial >= 0 && ms < LAST_DATE_MS)) return numberText(String(serial));
  const iso = new Date(ms).toISOString().slice(0, 19).replace("T", " ");
  const [isoDay, time] = iso.split(" ");
  // Excel shows the phantom day as the date it counts.
  const day = !date1904 && Math.floor(serial) === PHANTOM_LEAP_DAY ? "1900-02-29" : isoDay;
  if (Number.isInteger(serial)) return day;
  return serial < 1 ? time : `${day} ${time}`;
}

/** ``"BC"`` → 54: the zero-based column of a cell reference's letters. */
function columnIndex(reference: string): number {
  let index = 0;
  for (const char of reference) {
    const code = char.charCodeAt(0);
    if (code < 65 || code > 90) break;
    index = index * 26 + (code - 64);
  }
  return index - 1;
}

/** A plain number as Excel shows it by default: at most 15 significant digits. */
function numberText(value: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? String(Number(number.toPrecision(15))) : value;
}

function cellText(
  cell: Element,
  sharedStrings: string[],
  dated: Set<number>,
  date1904: boolean,
): string {
  const type = cell.getAttribute("t");
  if (type === "inlineStr") {
    const inline = descendants(cell, "is")[0];
    return inline ? runText(inline) : "";
  }
  const value = descendants(cell, "v")[0]?.textContent ?? "";
  if (value === "") {
    // A formula the writer saved without computing; show what it would compute.
    const formula = descendants(cell, "f")[0]?.textContent;
    return formula ? `=${formula}` : "";
  }
  if (type === "s") return sharedStrings[Number(value)] ?? "";
  if (type === "b") return value === "1" ? "TRUE" : "FALSE";
  if (type === "str" || type === "e" || type === "d") return value;
  if (dated.has(Number(cell.getAttribute("s") ?? -1))) {
    const serial = Number(value);
    if (Number.isFinite(serial)) return excelSerialToText(serial, date1904);
  }
  return numberText(value);
}

function sheetTable(
  sheet: Document,
  sharedStrings: string[],
  dated: Set<number>,
  date1904: boolean,
): DataTable | null {
  const records: string[][] = [];
  let totalRows = 0;
  let widest = 0;
  for (const row of descendants(sheet, "row")) {
    const cells = descendants(row, "c");
    // Rows that only carry formatting don't count.
    const values: string[] = [];
    let hasValue = false;
    let next = 0;
    for (const cell of cells) {
      const reference = cell.getAttribute("r");
      const column = reference ? columnIndex(reference) : next;
      next = column + 1;
      const text = cellText(cell, sharedStrings, dated, date1904);
      if (text === "") continue;
      // Counted however far right it is; kept only within the column cap.
      hasValue = true;
      widest = Math.max(widest, column + 1);
      if (column >= DATA_TABLE_MAX_COLUMNS) continue;
      while (values.length < column) values.push("");
      values[column] = text;
    }
    if (!hasValue) continue;
    if (records.length > 0) totalRows += 1;
    if (records.length <= DATA_TABLE_MAX_ROWS) records.push(values);
  }
  return tableFromRecords(records, totalRows, widest, "Excel workbook");
}

/** A sheet's ``r:id``, whatever prefix its relationships namespace has. */
function relationshipId(sheet: Element): string | undefined {
  return Array.from(sheet.attributes).find(
    (attribute) => attribute.localName === "id" && attribute.namespaceURI,
  )?.value;
}

/**
 * Read an Excel workbook's visible sheets for the table preview.
 *
 * @param bytes - The ``.xlsx`` file.
 * @returns Each visible sheet in workbook order, with its table.
 * @throws WorkbookError When the file can't be read, with a message to show.
 */
export async function readWorkbook(bytes: Uint8Array): Promise<WorkbookSheet[]> {
  try {
    return await readSheets(bytes);
  } catch (error) {
    if (error instanceof WorkbookError) throw error;
    // Offsets or sizes in a corrupt zip that point outside the file.
    throw new WorkbookError("This Excel workbook is damaged.");
  }
}

async function readSheets(bytes: Uint8Array): Promise<WorkbookSheet[]> {
  const entries = zipEntries(bytes);
  const workbook = await readPart(bytes, entries, "xl/workbook.xml");
  if (!workbook) throw new WorkbookError("This file isn't a readable Excel workbook.");
  const relationships = await readPart(bytes, entries, "xl/_rels/workbook.xml.rels");
  const targets = new Map<string, string>();
  for (const relationship of Array.from(
    relationships ? descendants(relationships, "Relationship") : [],
  )) {
    const target = relationship.getAttribute("Target") ?? "";
    targets.set(
      relationship.getAttribute("Id") ?? "",
      target.startsWith("/") ? target.slice(1) : `xl/${target}`,
    );
  }
  const shared = await readPart(bytes, entries, "xl/sharedStrings.xml");
  const sharedStrings = (shared ? descendants(shared, "si") : []).map(runText);
  const dated = dateStyles(await readPart(bytes, entries, "xl/styles.xml"));
  const properties = descendants(workbook, "workbookPr")[0];
  const date1904 = ["1", "true"].includes(properties?.getAttribute("date1904") ?? "");

  const sheets: WorkbookSheet[] = [];
  // One sheet at a time, so only one inflated part is held at once.
  /* oxlint-disable no-await-in-loop */
  for (const sheet of descendants(workbook, "sheet")) {
    if (["hidden", "veryHidden"].includes(sheet.getAttribute("state") ?? "")) continue;
    const target = targets.get(relationshipId(sheet) ?? "");
    const part = target ? await readPart(bytes, entries, target) : null;
    sheets.push({
      name: sheet.getAttribute("name") ?? `Sheet ${sheets.length + 1}`,
      table: part ? sheetTable(part, sharedStrings, dated, date1904) : null,
    });
  }
  /* oxlint-enable no-await-in-loop */
  return sheets;
}
