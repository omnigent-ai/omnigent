// Parquet reading for the table preview, in the browser with hyparquet.

// Types only: the library itself loads on first use.
import type * as Hyparquet from "hyparquet";
import {
  DATA_TABLE_MAX_COLUMNS,
  DATA_TABLE_MAX_ROWS,
  tableFromRecords,
  type DataTable,
} from "./dataTable";

/** Codecs hyparquet decodes without extra libraries. */
const READABLE_CODECS = new Set(["UNCOMPRESSED", "SNAPPY"]);

/** A Parquet file that can't be read, with a message for the person viewing it. */
export class ParquetError extends Error {}

/** A Parquet value as the table shows it, e.g. ``2026-03-01`` for a DATE column. */
export function parquetCellText(value: unknown, isDate: boolean): string {
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
  if (typeof value === "object") {
    return JSON.stringify(value, (_key, inner: unknown) =>
      typeof inner === "bigint" ? inner.toString() : inner,
    );
  }
  return String(value);
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
export async function readParquet(bytes: Uint8Array): Promise<DataTable | null> {
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
  const schema = parquetSchema(metadata).children.map((column) => column.element);
  if (schema.length === 0) return null;
  // Only the kept columns are read; the rest are counted.
  const columns = schema.slice(0, DATA_TABLE_MAX_COLUMNS);
  const rows = await parquetReadObjects({
    file,
    rowEnd: DATA_TABLE_MAX_ROWS,
    columns: columns.map((column) => column.name),
  });
  const isDate = columns.map(
    (column) => column.converted_type === "DATE" || column.logical_type?.type === "DATE",
  );
  const records = [
    columns.map((column) => column.name),
    ...rows.map((row) =>
      columns.map((column, index) => parquetCellText(row[column.name], isDate[index])),
    ),
  ];
  return tableFromRecords(records, Number(metadata.num_rows), schema.length, "Parquet");
}
