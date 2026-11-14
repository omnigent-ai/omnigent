import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
  type UIEvent,
} from "react";
import { ArrowDownIcon, ArrowUpIcon, ArrowUpDownIcon, CopyIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import {
  compareCells,
  decodeDataFile,
  parseDataTable,
  type DataColumnType,
  type DataTable,
} from "./dataTable";

// Sorting and filtering apply only to the rows the table keeps (see dataTable.ts).

type SortDirection = "ascending" | "descending";

const TYPE_LABELS: Record<DataColumnType, string> = {
  number: "number",
  date: "date",
  text: "text",
};

const DELIMITER_LABELS: Record<string, string> = {
  ",": "comma-separated",
  ";": "semicolon-separated",
  "\t": "tab-separated",
  "|": "pipe-separated",
};

/** Widest a column starts out; dragging its edge can widen it further. */
const DEFAULT_MAX_COLUMN_WIDTH = 512;
const MIN_COLUMN_WIDTH = 64;
const MAX_COLUMN_WIDTH = 2048;
const KEYBOARD_RESIZE_STEP = 16;

async function copyWithToast(text: string, what: string) {
  try {
    await copyText(text);
    toast.success(`Copied ${what}`);
  } catch {
    toast.error(`Couldn't copy the ${what}`);
  }
}

/** A row as tab-separated text, quoting cells that hold a tab, newline or quote. */
function rowAsTsv(row: string[]): string {
  return row
    .map((cell) => (/[\t\n\r"]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell))
    .join("\t");
}

export function DataTablePreview({
  content,
  encoding,
  path,
  truncated,
  rootRef,
  onScroll,
  onRowsChange,
}: {
  content: string;
  /** How the server sent *content*: UTF-8 text, or base64 for anything else. */
  encoding: "utf-8" | "base64";
  path: string;
  /** The server sent only the start of the file, so the row count is a lower bound. */
  truncated: boolean;
  rootRef?: RefObject<HTMLDivElement | null>;
  onScroll?: (event: UIEvent<HTMLElement>) => void;
  /** Called when the rows on screen change (sorted, filtered or reloaded), e.g. to refresh find. */
  onRowsChange?: () => void;
}) {
  const table = useMemo(() => {
    const decoded = decodeDataFile(content, encoding);
    return parseDataTable(decoded.text, path, decoded.encoding);
  }, [content, encoding, path]);

  if (!table) {
    return (
      <div className="p-8 text-ui text-muted-foreground">
        This file has no rows. Switch to the source view to inspect the raw file.
      </div>
    );
  }
  return (
    <DataTableView
      // Sorting, filters and widths belong to these columns; a rewrite with others starts afresh.
      key={table.header.join("\u0000")}
      table={table}
      path={path}
      truncated={truncated}
      rootRef={rootRef}
      onScroll={onScroll}
      onRowsChange={onRowsChange}
    />
  );
}

function DataTableView({
  table,
  path,
  truncated,
  rootRef,
  onScroll,
  onRowsChange,
}: {
  table: DataTable;
  path: string;
  truncated: boolean;
  rootRef?: RefObject<HTMLDivElement | null>;
  onScroll?: (event: UIEvent<HTMLElement>) => void;
  onRowsChange?: () => void;
}) {
  const [sort, setSort] = useState<{ column: number; direction: SortDirection } | null>(null);
  const [filters, setFilters] = useState<string[]>([]);
  const [widths, setWidths] = useState<Record<number, number>>({});
  // Rendered widths of auto-sized columns, read when their resize handle takes focus.
  const [fittedWidths, setFittedWidths] = useState<Record<number, number>>({});
  // Tied to the rows it was picked from, so a refreshed file drops a stale selection.
  const [picked, setPicked] = useState<{
    rows: string[][];
    row: string[];
    column: number;
  } | null>(null);
  const selected = picked?.rows === table.rows ? picked : null;

  const visibleRows = useMemo(() => {
    const active = filters
      .map((filter, column) => ({ needle: filter.trim().toLowerCase(), column }))
      .filter(({ needle }) => needle !== "");
    const rows = active.length
      ? table.rows.filter((row) =>
          active.every(({ needle, column }) => row[column].toLowerCase().includes(needle)),
        )
      : table.rows;
    if (!sort) return rows;
    const type = table.columnTypes[sort.column];
    const sign = sort.direction === "ascending" ? 1 : -1;
    // Empty cells stay last in either direction.
    return [...rows].sort((a, b) => {
      const aCell = a[sort.column];
      const bCell = b[sort.column];
      if (aCell.trim() === "" || bCell.trim() === "") return compareCells(aCell, bCell, type);
      return sign * compareCells(aCell, bCell, type);
    });
  }, [table, filters, sort]);
  useEffect(() => onRowsChange?.(), [visibleRows, onRowsChange]);

  const toggleSort = (column: number) =>
    setSort((current) => {
      if (current?.column !== column) return { column, direction: "ascending" };
      return current.direction === "ascending" ? { column, direction: "descending" } : null;
    });
  const setFilter = (column: number, value: string) =>
    setFilters((current) => {
      const next = [...current];
      next[column] = value;
      return next;
    });
  const setWidth = (column: number, width: number) =>
    setWidths((current) => ({
      ...current,
      [column]: Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, width)),
    }));

  const startResize = (column: number, event: PointerEvent<HTMLDivElement>) => {
    const header = event.currentTarget.parentElement;
    if (!header) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = header.getBoundingClientRect().width;
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const move = (moved: globalThis.PointerEvent) =>
      setWidth(column, startWidth + moved.clientX - startX);
    const stop = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", stop);
      handle.removeEventListener("pointercancel", stop);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
  };
  const measureFitted = (column: number, event: FocusEvent<HTMLDivElement>) => {
    const rendered = event.currentTarget.parentElement?.getBoundingClientRect().width ?? 0;
    const width = Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, Math.round(rendered)));
    setFittedWidths((current) =>
      current[column] === width ? current : { ...current, [column]: width },
    );
  };
  const resizeByKey = (column: number, event: KeyboardEvent<HTMLDivElement>) => {
    const step =
      event.key === "ArrowRight"
        ? KEYBOARD_RESIZE_STEP
        : event.key === "ArrowLeft"
          ? -KEYBOARD_RESIZE_STEP
          : 0;
    if (!step) return;
    event.preventDefault();
    const header = event.currentTarget.parentElement;
    setWidth(column, (widths[column] ?? header?.getBoundingClientRect().width ?? 0) + step);
  };
  const pick = useCallback(
    (row: string[], column: number) => setPicked({ rows: table.rows, row, column }),
    [table.rows],
  );
  // One cell is a tab stop: the selected one while it's on screen, else the first.
  const selectedIndex = selected ? visibleRows.indexOf(selected.row) : -1;
  const tabStop =
    selected && selectedIndex >= 0
      ? { index: selectedIndex, column: selected.column }
      : { index: 0, column: 0 };
  const moveFocus = (event: KeyboardEvent<HTMLTableSectionElement>) => {
    const cell = (event.target as HTMLElement).closest<HTMLElement>("td[data-cell]");
    if (!cell) return;
    const [index, column] = cell.dataset.cell!.split(":").map(Number);
    const last = { index: visibleRows.length - 1, column: table.header.length - 1 };
    const next = {
      ArrowUp: [Math.max(0, index - 1), column],
      ArrowDown: [Math.min(last.index, index + 1), column],
      ArrowLeft: [index, Math.max(0, column - 1)],
      ArrowRight: [index, Math.min(last.column, column + 1)],
      Home: [index, 0],
      End: [index, last.column],
    }[event.key];
    if (!next) return;
    event.preventDefault();
    event.currentTarget
      .querySelector<HTMLElement>(`td[data-cell="${next[0]}:${next[1]}"]`)
      ?.focus();
  };

  const shown = table.rows.length;
  const rowCount = `${truncated ? "At least " : ""}${table.totalRows.toLocaleString()} ${
    table.totalRows === 1 ? "row" : "rows"
  }`;
  const filtered = visibleRows.length !== shown;
  const columnsCut = table.header.length < table.totalColumns;
  const notes = [
    shown < table.totalRows &&
      (truncated
        ? `the first ${shown.toLocaleString()} rows`
        : `the first ${shown.toLocaleString()} of ${table.totalRows.toLocaleString()} rows`),
    columnsCut &&
      `the first ${table.header.length.toLocaleString()} of ${table.totalColumns.toLocaleString()} columns`,
  ].filter((note): note is string => typeof note === "string");

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-sm text-muted-foreground">
        <span data-testid="data-table-summary">
          {rowCount} · {table.totalColumns.toLocaleString()}{" "}
          {table.totalColumns === 1 ? "column" : "columns"} ·{" "}
          {DELIMITER_LABELS[table.delimiter] ?? "delimited"}
          {table.encoding !== "utf-8" ? ` · ${table.encoding}` : ""}
        </span>
        {filtered && <span>{visibleRows.length.toLocaleString()} matching</span>}
        {selected && selectedIndex >= 0 && (
          // The selection is only an outline on screen, so the buttons name what they copy.
          <span className="ml-auto flex gap-1">
            <Button
              variant="ghost"
              size="xs"
              aria-label={`Copy cell (${table.header[selected.column] || `column ${selected.column + 1}`}, row ${(selectedIndex + 1).toLocaleString()})`}
              onClick={() => copyWithToast(selected.row[selected.column], "cell")}
            >
              <CopyIcon /> Copy cell
            </Button>
            <Button
              variant="ghost"
              size="xs"
              aria-label={`Copy row ${(selectedIndex + 1).toLocaleString()}`}
              onClick={() => copyWithToast(rowAsTsv(selected.row), "row")}
            >
              <CopyIcon /> Copy row
            </Button>
          </span>
        )}
      </div>
      {notes.length > 0 && (
        // A truncated file also gets the viewer's shared truncation banner.
        <div role="note" className="border-b bg-muted/40 px-4 py-1.5 text-sm text-muted-foreground">
          {`Showing ${notes.join(" and ")}.`}
          {truncated ? "" : ` Download the file to see ${columnsCut ? "all of it" : "every row"}.`}
        </div>
      )}
      {/* Only the table scrolls, so the summary and its copy buttons stay in view. */}
      <div
        ref={rootRef}
        data-preview-scroll
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-auto"
      >
        <table aria-label={path} className="border-separate border-spacing-0 font-mono text-sm">
          <thead data-preview-sticky className="sticky top-0 z-10 bg-background">
            <tr>
              {table.header.map((name, column) => {
                const sorted = sort?.column === column ? sort.direction : undefined;
                const SortIcon =
                  sorted === "ascending"
                    ? ArrowUpIcon
                    : sorted === "descending"
                      ? ArrowDownIcon
                      : ArrowUpDownIcon;
                const width = widths[column];
                return (
                  <th
                    // eslint-disable-next-line react/no-array-index-key
                    key={column}
                    scope="col"
                    aria-sort={sorted ?? "none"}
                    style={
                      width === undefined
                        ? { maxWidth: DEFAULT_MAX_COLUMN_WIDTH }
                        : { width, minWidth: width, maxWidth: width }
                    }
                    className="relative min-w-24 overflow-hidden border-r border-b px-2 py-1 text-left align-top font-sans font-medium"
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(column)}
                      className="flex w-full items-center gap-1 text-left"
                      aria-label={`Sort by ${name}`}
                    >
                      <span className="truncate">{name}</span>
                      <SortIcon className="size-3.5 shrink-0 opacity-60" />
                    </button>
                    <span className="text-xs font-normal text-muted-foreground">
                      {TYPE_LABELS[table.columnTypes[column]]}
                    </span>
                    <Input
                      aria-label={`Filter ${name}`}
                      placeholder="Filter"
                      value={filters[column] ?? ""}
                      onChange={(event) => setFilter(column, event.target.value)}
                      className="mt-1 h-6 text-xs"
                    />
                    <div
                      role="separator"
                      aria-orientation="vertical"
                      aria-label={`Resize ${name}`}
                      aria-valuemin={MIN_COLUMN_WIDTH}
                      aria-valuemax={MAX_COLUMN_WIDTH}
                      aria-valuenow={Math.round(width ?? fittedWidths[column] ?? MIN_COLUMN_WIDTH)}
                      aria-valuetext={
                        width === undefined ? "Fits its content" : `${Math.round(width)} pixels`
                      }
                      tabIndex={0}
                      onFocus={(event) => measureFitted(column, event)}
                      onPointerDown={(event) => startResize(column, event)}
                      onKeyDown={(event) => resizeByKey(column, event)}
                      className="absolute inset-y-0 right-0 w-1.5 cursor-col-resize touch-none hover:bg-ring/40 focus-visible:bg-ring/60 focus-visible:outline-none"
                    />
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody onKeyDown={moveFocus}>
            {visibleRows.map((row, index) => (
              <TableRow
                // eslint-disable-next-line react/no-array-index-key
                key={index}
                row={row}
                index={index}
                columnTypes={table.columnTypes}
                widths={widths}
                selectedColumn={selected?.row === row ? selected.column : null}
                tabColumn={tabStop.index === index ? tabStop.column : null}
                onPick={pick}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** One body row; memoized so moving the selection re-renders only the rows it leaves and enters. */
const TableRow = memo(function TableRow({
  row,
  index,
  columnTypes,
  widths,
  selectedColumn,
  tabColumn,
  onPick,
}: {
  row: string[];
  index: number;
  columnTypes: DataColumnType[];
  widths: Record<number, number>;
  /** The selected cell's column, when the selection is in this row. */
  selectedColumn: number | null;
  /** The column of the table's single tab stop, when it is in this row. */
  tabColumn: number | null;
  onPick: (row: string[], column: number) => void;
}) {
  return (
    <tr className="hover:bg-muted/40">
      {row.map((cell, column) => {
        const isSelected = selectedColumn === column;
        return (
          <td
            // eslint-disable-next-line react/no-array-index-key
            key={column}
            data-cell={`${index}:${column}`}
            tabIndex={tabColumn === column ? 0 : -1}
            onClick={() => onPick(row, column)}
            onFocus={() => onPick(row, column)}
            data-selected={isSelected || undefined}
            style={{ maxWidth: widths[column] ?? DEFAULT_MAX_COLUMN_WIDTH }}
            className={cn(
              "truncate border-r border-b px-2 py-0.5 whitespace-pre focus-visible:outline-none",
              columnTypes[column] === "number" && "text-right tabular-nums",
              isSelected && "outline outline-2 -outline-offset-2 outline-ring",
            )}
            title={cell}
          >
            {cell}
          </td>
        );
      })}
    </tr>
  );
});
