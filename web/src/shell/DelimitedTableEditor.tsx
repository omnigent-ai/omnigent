import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type RefObject,
} from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { DataTablePreview, DataTableView } from "./DataTablePreview";
import { editableDataTable, replaceDelimitedCell } from "./dataTable";
import type { SaveStatus } from "./codeViewerHelpers";
import { useEditorAutoSave } from "./useEditorAutoSave";
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";

interface Props extends ComponentProps<typeof DataTablePreview> {
  conversationId: string;
  canEdit: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  onSaveStatusChange?: (status: SaveStatus) => void;
}

/** Host-owned saving and conflict handling; the table only reports which cell to edit. */
export function DelimitedTableEditor(props: Props) {
  const setContentRef = useRef<((content: string) => void) | null>(null);
  const sync = useMarkdownEditorSync({
    content: props.content,
    path: props.path,
    isSettled: true,
    onDirtyChange: props.onDirtyChange,
    setContentRef,
  });
  return <Editor key={sync.editorKey} {...props} sync={sync} setContentRef={setContentRef} />;
}

function Editor({
  content,
  encoding,
  path,
  truncated,
  conversationId,
  canEdit,
  rootRef,
  onScroll,
  onRowsChange,
  onSaveStatusChange,
  sync,
  setContentRef,
}: Props & {
  sync: ReturnType<typeof useMarkdownEditorSync>;
  setContentRef: RefObject<((content: string) => void) | null>;
}) {
  const [draft, setDraft] = useState(content);
  const [format, setFormat] = useState({ encoding, truncated });
  const latest = useRef(content);
  const baseline = useRef<string | null>(content);
  const [previous, setPrevious] = useState<string | null>(null);
  const [cell, setCell] = useState<{
    row: number;
    column: number;
    name: string;
    value: string;
    source: string;
  } | null>(null);
  const [error, setError] = useState("");
  const table = useMemo(
    () =>
      format.encoding === "utf-8" && !format.truncated ? editableDataTable(draft, path) : null,
    [draft, path, format],
  );
  const editable = canEdit && table !== null;
  const writable = editable && encoding === "utf-8" && !truncated;
  const isEditorDirty = useCallback(() => latest.current !== baseline.current, []);
  useLayoutEffect(() => {
    setContentRef.current = (next) => {
      latest.current = next;
      baseline.current = next;
      setDraft(next);
      setFormat({ encoding, truncated });
      setPrevious(null);
    };
    return () => {
      setContentRef.current = null;
    };
  }, [setContentRef, encoding, truncated]);
  const { autoSave, saveDisabled, writeFile } = useEditorAutoSave({
    conversationId,
    path,
    canEdit: writable,
    isDirty: sync.isDirty,
    setDirty: sync.setDirty,
    hasExternalUpdate: sync.hasExternalUpdate,
    markSaved: sync.markSaved,
    beginServerCheck: sync.beginServerCheck,
    dismissExternalUpdate: sync.dismissExternalUpdate,
    baselineRef: baseline,
    getContent: () => latest.current,
    isEditorDirty,
  });

  const status: SaveStatus =
    writeFile.isError && sync.isDirty
      ? "error"
      : writeFile.isPending
        ? "saving"
        : saveDisabled && sync.isDirty
          ? "offline"
          : sync.isDirty
            ? "unsaved"
            : writeFile.isSuccess
              ? "saved"
              : "idle";
  useEffect(() => onSaveStatusChange?.(status), [onSaveStatusChange, status]);
  useEffect(() => () => onSaveStatusChange?.("idle"), [onSaveStatusChange]);

  const changeContent = (next: string) => {
    latest.current = next;
    setDraft(next);
    const dirty = isEditorDirty();
    sync.setDirty(dirty);
    if (dirty) autoSave.schedule();
    else autoSave.cancel();
  };
  const editCell = useCallback(
    (row: number, column: number) => {
      if (!editable || !table) return;
      setError("");
      setCell({
        row,
        column,
        name: table.header[column],
        value: table.rows[row][column],
        source: latest.current,
      });
    },
    [editable, table],
  );
  const applyCell = () => {
    if (!cell || !editable) return;
    if (cell.source !== latest.current) {
      setError(
        "The file changed while this cell was open. Cancel and reopen the cell to edit the latest version.",
      );
      return;
    }
    try {
      const next = replaceDelimitedCell(latest.current, path, cell.row, cell.column, cell.value);
      if (next !== latest.current) {
        setPrevious(latest.current);
        changeContent(next);
      }
      setCell(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "This cell could not be changed.");
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {sync.hasExternalUpdate && sync.isDirty && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 border-b bg-warning/10 px-4 py-2 text-sm"
        >
          <span>This file was modified externally while you were editing.</span>
          <Button
            variant="outline"
            size="sm"
            disabled={!writable}
            onClick={sync.dismissExternalUpdate}
          >
            Keep mine
          </Button>
          <Button size="sm" onClick={sync.discardAndApplyExternal}>
            Load latest
          </Button>
        </div>
      )}
      {canEdit && (
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2 text-sm text-muted-foreground">
          <span className="flex-1">
            {editable
              ? "Select a cell to edit it. Applied changes save automatically."
              : "Table editing requires a complete, untrimmed UTF-8 file with even rows."}
          </span>
          {editable && previous !== null && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                changeContent(previous);
                setPrevious(null);
              }}
            >
              Undo last edit
            </Button>
          )}
          {editable && sync.isDirty && !sync.hasExternalUpdate && (
            <Button
              variant="outline"
              size="sm"
              disabled={!writable || saveDisabled || writeFile.isPending}
              onClick={autoSave.flush}
            >
              {writeFile.isError ? "Retry save" : "Save now"}
            </Button>
          )}
        </div>
      )}
      {writeFile.isError && sync.isDirty && (
        <div role="alert" className="border-b px-4 py-2 text-sm text-destructive">
          Changes could not be saved. Your edits are still here; retry saving when the workspace is
          available.
        </div>
      )}
      {saveDisabled && sync.isDirty && (
        <div role="status" className="border-b px-4 py-2 text-sm text-muted-foreground">
          Offline. Changes will save when the workspace reconnects.
        </div>
      )}
      <div className="min-h-0 flex-1">
        {table ? (
          <DataTableView
            key={table.header.join("\u0000")}
            table={table}
            path={path}
            truncated={truncated}
            rootRef={rootRef}
            onScroll={onScroll}
            onRowsChange={onRowsChange}
            onEditCell={editable ? editCell : undefined}
          />
        ) : (
          <DataTablePreview
            content={draft}
            encoding={format.encoding}
            path={path}
            truncated={format.truncated}
            rootRef={rootRef}
            onScroll={onScroll}
            onRowsChange={onRowsChange}
          />
        )}
      </div>
      <Dialog
        open={cell !== null}
        onOpenChange={(open) => {
          if (!open) setCell(null);
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              applyCell();
            }}
          >
            <DialogHeader>
              <DialogTitle>Edit {cell?.name ?? "cell"}</DialogTitle>
              <DialogDescription>
                Apply the value to save it to the file. Cancel leaves the cell unchanged.
              </DialogDescription>
            </DialogHeader>
            <Textarea
              aria-label="Cell value"
              className="my-4 font-mono"
              value={cell?.value ?? ""}
              onChange={(event) => {
                setError("");
                setCell((current) => current && { ...current, value: event.target.value });
              }}
            />
            {error && (
              <p role="alert" className="mb-4 text-sm text-destructive">
                {error}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCell(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!editable}>
                Apply
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
