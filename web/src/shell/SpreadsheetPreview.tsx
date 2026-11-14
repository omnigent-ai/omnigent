import { useEffect, useState, type KeyboardEvent, type RefObject, type UIEvent } from "react";
import { cn } from "@/lib/utils";
import { decodeBase64 } from "./dataTable";
import { DataTableView } from "./DataTablePreview";
import { ParquetError, readParquet } from "./parquet";
import { readWorkbook, WorkbookError, type WorkbookSheet } from "./xlsx";

// ---------------------------------------------------------------------------
// SpreadsheetPreview — read-only table view of an Excel workbook (a tab per
// sheet) or a Parquet file, read in the browser from the bytes the viewer has.
// ---------------------------------------------------------------------------

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; sheets: WorkbookSheet[] };

const isParquet = (path: string) => path.toLowerCase().endsWith(".parquet");

async function readSpreadsheet(
  content: string,
  encoding: "utf-8" | "base64",
  path: string,
): Promise<WorkbookSheet[]> {
  // A file whose bytes happen to be valid UTF-8 can arrive as text, not base64.
  const bytes = encoding === "base64" ? decodeBase64(content) : new TextEncoder().encode(content);
  if (isParquet(path)) return [{ name: "Parquet", table: await readParquet(bytes) }];
  return readWorkbook(bytes);
}

export function SpreadsheetPreview({
  content,
  encoding,
  path,
  truncated,
  rootRef,
  onScroll,
  onRowsChange,
}: {
  /** The file's bytes, base64-encoded as the server sends a binary file. */
  content: string;
  /** How the server sent *content*: base64, or UTF-8 text when the bytes allowed it. */
  encoding: "utf-8" | "base64";
  path: string;
  /** The server sent only the start of the file. */
  truncated: boolean;
  rootRef?: RefObject<HTMLDivElement | null>;
  onScroll?: (event: UIEvent<HTMLElement>) => void;
  onRowsChange?: () => void;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [active, setActive] = useState(0);
  // Switching sheets, or the file loading, failing or emptying on a refresh,
  // changes what find searches.
  useEffect(() => onRowsChange?.(), [active, state, onRowsChange]);

  useEffect(() => {
    // Both formats keep their index at the end of the file, which a partial read lacks.
    if (truncated) {
      setState({
        status: "error",
        message:
          "This file is larger than the preview limit, so it can't be read here. Download it to open it.",
      });
      return;
    }
    let current = true;
    setState({ status: "loading" });
    readSpreadsheet(content, encoding, path).then(
      (sheets) => {
        if (!current) return;
        setActive(0);
        setState({ status: "ready", sheets });
      },
      (error: unknown) => {
        if (!current) return;
        const known = error instanceof WorkbookError || error instanceof ParquetError;
        setState({
          status: "error",
          message: known
            ? error.message
            : "This file couldn't be read. Download it to open it elsewhere.",
        });
      },
    );
    return () => {
      current = false;
    };
  }, [content, encoding, path, truncated]);

  if (state.status === "loading") {
    return <div className="p-8 text-ui text-muted-foreground">Reading the file…</div>;
  }
  if (state.status === "error") {
    return (
      <div role="alert" className="p-8 text-ui text-muted-foreground">
        {state.message}
      </div>
    );
  }
  const { sheets } = state;
  if (sheets.length === 0) {
    return <div className="p-8 text-ui text-muted-foreground">This workbook has no sheets.</div>;
  }
  const index = Math.min(active, sheets.length - 1);
  const sheet = sheets[index];

  const moveTab = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = (index + step + sheets.length) % sheets.length;
    setActive(next);
    event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
  };

  return (
    <div className="flex h-full flex-col">
      {sheets.length > 1 && (
        <div
          role="tablist"
          aria-label="Sheets"
          onKeyDown={moveTab}
          className="flex shrink-0 gap-1 overflow-x-auto border-b px-2 pt-1.5"
        >
          {sheets.map((candidate, candidateIndex) => (
            <button
              // eslint-disable-next-line react/no-array-index-key
              key={candidateIndex}
              type="button"
              role="tab"
              aria-selected={candidateIndex === index}
              tabIndex={candidateIndex === index ? 0 : -1}
              onClick={() => setActive(candidateIndex)}
              className={cn(
                "shrink-0 rounded-t-md border border-b-0 px-3 py-1 text-sm",
                candidateIndex === index
                  ? "bg-background font-medium text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {candidate.name}
            </button>
          ))}
        </div>
      )}
      <div role={sheets.length > 1 ? "tabpanel" : undefined} className="min-h-0 flex-1">
        {sheet.table ? (
          <DataTableView
            // A sheet keeps its own sort, filters and widths only while it is shown.
            key={index}
            table={sheet.table}
            path={sheets.length > 1 ? `${path}, sheet ${sheet.name}` : path}
            truncated={false}
            rootRef={rootRef}
            onScroll={onScroll}
            onRowsChange={onRowsChange}
          />
        ) : (
          <div
            role={sheet.message ? "alert" : undefined}
            className="p-8 text-ui text-muted-foreground"
          >
            {sheet.message ?? "This sheet is empty."}
          </div>
        )}
      </div>
    </div>
  );
}
