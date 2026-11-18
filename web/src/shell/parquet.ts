// Parquet reading for the table preview, in the browser with hyparquet.

// Types only: the library itself loads on first use.
import type * as Hyparquet from "hyparquet";
import {
  DATA_TABLE_MAX_CHARS,
  DATA_TABLE_MAX_COLUMNS,
  DATA_TABLE_MAX_NAME_CHARS,
  DATA_TABLE_MAX_ROWS,
  tableFromRecords,
  type DataTable,
} from "./dataTable";

/**
 * Most values in the selected chunks: the reader decodes whole pages before
 * slicing to the requested rows, before the table's text budget applies.
 */
export const PARQUET_MAX_VALUES = 10_000_000;
/** Most uncompressed bytes in the chunks the preview may decode. */
export const PARQUET_MAX_BYTES = 64 * 1024 * 1024;

/** What a nested column shows when its values are past {@link PARQUET_MAX_VALUES}. */
export const SKIPPED_NESTED_VALUE = "(too many values to preview)";

/** Codecs hyparquet decodes without extra libraries. */
const READABLE_CODECS = new Set(["UNCOMPRESSED", "SNAPPY"]);

/** A Parquet file that can't be read, with a message for the person viewing it. */
export class ParquetError extends Error {}

/** Thrown by {@link boundedJson} once its text passes the limit. */
const PAST_LIMIT = Symbol("past limit");

/**
 * *value* as JSON, stopping once the text passes *limit* characters: a list
 * repeating one long string can describe far more text than the file holds.
 */
function boundedJson(value: unknown, limit: number): string {
  const parts: string[] = [];
  let length = 0;
  const emit = (text: string) => {
    parts.push(text);
    length += text.length;
    if (length > limit) throw PAST_LIMIT;
  };
  const write = (inner: unknown) => {
    if (typeof inner === "bigint") emit(JSON.stringify(inner.toString()));
    else if (inner instanceof Uint8Array) emit(JSON.stringify(`(${inner.length} bytes)`));
    else if (Array.isArray(inner)) {
      emit("[");
      inner.forEach((item: unknown, index) => {
        if (index > 0) emit(",");
        write(item === undefined ? null : item);
      });
      emit("]");
    } else if (inner !== null && typeof inner === "object" && !(inner instanceof Date)) {
      emit("{");
      let first = true;
      for (const [key, item] of Object.entries(inner)) {
        if (item === undefined) continue;
        emit(`${first ? "" : ","}${JSON.stringify(key)}:`);
        first = false;
        write(item);
      }
      emit("}");
    } else if (typeof inner === "string") {
      // A long string is cut before it is quoted, so it isn't copied whole.
      emit(JSON.stringify(inner.slice(0, limit + 1)));
    } else emit(JSON.stringify(inner) ?? "null");
  };
  try {
    write(value);
  } catch (error) {
    if (error !== PAST_LIMIT) throw error;
    return `${parts.join("").slice(0, limit)}…`;
  }
  return parts.join("");
}

/**
 * A Parquet value as the table shows it, e.g. ``2026-03-01`` for a DATE column.
 *
 * @param limit - Most characters to build for a nested value; past it the text
 *   is cut and ends in ``…``.
 */
export function parquetCellText(
  value: unknown,
  isDate: boolean,
  limit = Number.POSITIVE_INFINITY,
): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Date) {
    // A sentinel such as the largest INT64 timestamp is past what a Date holds.
    if (Number.isNaN(value.getTime())) return "(out-of-range date)";
    const iso = value.toISOString();
    if (isDate) return iso.slice(0, 10);
    return iso.endsWith("T00:00:00.000Z") ? iso.slice(0, 10) : iso.replace(".000Z", "Z");
  }
  if (value instanceof Uint8Array) return `(${value.length} bytes)`;
  if (typeof value === "object") return boundedJson(value, limit);
  return String(value);
}

/**
 * Bound whole chunks in the selected row groups, since even a flat column's
 * first page can hold more values than the requested rows.
 */
function withinReadBudget(
  metadata: Hyparquet.FileMetaData,
  columns: Set<string>,
  maxValues: number,
): boolean {
  let rows = 0;
  let values = 0;
  let bytes = 0;
  for (const group of metadata.row_groups) {
    if (rows >= DATA_TABLE_MAX_ROWS) break;
    const groupRows = Number(group.num_rows);
    if (!Number.isFinite(groupRows) || groupRows < 0) return false;
    if (groupRows === 0) continue;
    rows += groupRows;
    for (const chunk of group.columns) {
      const meta = chunk.meta_data;
      if (!meta) return false;
      if (!columns.has(meta.path_in_schema[0])) continue;
      const chunkValues = Number(meta.num_values);
      const chunkBytes = Number(meta.total_uncompressed_size);
      if (!Number.isFinite(chunkValues) || chunkValues < 0) return false;
      if (!Number.isFinite(chunkBytes) || chunkBytes < 0) return false;
      values += chunkValues;
      bytes += chunkBytes;
      if (values > maxValues || bytes > PARQUET_MAX_BYTES) return false;
    }
  }
  return true;
}

/**
 * Read a Parquet file's schema and first rows for the table preview.
 *
 * The row count comes from the file's footer, so it is exact.
 *
 * @param bytes - The whole ``.parquet`` file.
 * @returns The table, or ``null`` for a file with no columns.
 * @throws ParquetError When the file can't be read, with a message to show.
 */
export async function readParquet(
  bytes: Uint8Array,
  maxValues = PARQUET_MAX_VALUES,
): Promise<DataTable | null> {
  const file =
    bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
      ? (bytes.buffer as ArrayBuffer)
      : (bytes.slice().buffer as ArrayBuffer);
  // Loaded on first use, so it stays out of the bundle until a Parquet file opens.
  let reader: typeof Hyparquet;
  try {
    reader = await import("hyparquet");
  } catch {
    throw new ParquetError(
      "The Parquet reader couldn't load. Check your connection, then reopen the file.",
    );
  }
  const { parquetMetadata, parquetReadObjects, parquetSchema } = reader;
  let metadata: ReturnType<typeof parquetMetadata>;
  try {
    metadata = parquetMetadata(file);
  } catch {
    throw new ParquetError("This file isn't a readable Parquet file.");
  }
  const unreadable = metadata.row_groups
    .flatMap((group) => group.columns.map((column) => column.meta_data?.codec))
    .find((codec) => codec !== undefined && !READABLE_CODECS.has(codec));
  if (unreadable) {
    throw new ParquetError(
      `This file is compressed with ${unreadable}, which the preview can't read yet. Download it to open it.`,
    );
  }
  const tree = parquetSchema(metadata).children;
  const schema = tree.map((column) => column.element);
  if (schema.length === 0) return null;
  // Only the kept columns are read; the rest are counted.
  const columns = schema.slice(0, DATA_TABLE_MAX_COLUMNS);
  const nested = new Set(
    tree
      .slice(0, DATA_TABLE_MAX_COLUMNS)
      .filter((node) => node.element.repetition_type === "REPEATED" || node.children.length > 0)
      .map((node) => node.element.name),
  );
  const columnNames = columns.map((column) => column.name);
  // Past the budget, skip nested columns first so flat columns can still show.
  const skipNested = !withinReadBudget(metadata, new Set(columnNames), maxValues);
  const readColumns = columnNames.filter((name) => !(skipNested && nested.has(name)));
  if (skipNested && !withinReadBudget(metadata, new Set(readColumns), maxValues)) {
    throw new ParquetError(
      "Reading this file would exceed the preview's memory limit. Download it to open it.",
    );
  }
  // With every kept column skipped there is nothing to read, but the rows still show.
  const rows =
    readColumns.length > 0
      ? await parquetReadObjects({
          file,
          rowEnd: DATA_TABLE_MAX_ROWS,
          columns: readColumns,
          utf8: false,
        })
      : Array.from(
          { length: Math.min(Number(metadata.num_rows), DATA_TABLE_MAX_ROWS) },
          (): Record<string, unknown> => ({}),
        );
  const isDate = columns.map(
    (column) => column.converted_type === "DATE" || column.logical_type?.type === "DATE",
  );
  const header = columns.map((column) => column.name);
  const records = [header];
  // Cells are built only while the table's text budget lasts; the row that
  // passes it is the last one built, and the table keeps or cuts it.
  // Charged as the table keeps the names, cut to the name limit.
  let budget =
    DATA_TABLE_MAX_CHARS -
    header.reduce((sum, name) => sum + Math.min(name.length, DATA_TABLE_MAX_NAME_CHARS), 0);
  for (const row of rows) {
    if (budget < 0) break;
    records.push(
      columns.map((column, index) => {
        const text =
          skipNested && nested.has(column.name)
            ? SKIPPED_NESTED_VALUE
            : parquetCellText(row[column.name], isDate[index], Math.max(budget, 0) + 1);
        budget -= text.length;
        return text;
      }),
    );
  }
  return tableFromRecords(records, Number(metadata.num_rows), schema.length, "Parquet");
}
