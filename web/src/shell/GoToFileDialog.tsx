// Go to File: open a file in the routed session's workspace by part of its name
// or path. The server returns substring matches (at most 500), ranked here; the
// dialog says when that list may be incomplete instead of implying it's all.

import { useEffect, useMemo, useRef, useState } from "react";

import { Command, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useWorkspaceFileSearch, useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";

import { type MatchRange, rankGoToFileResults, stillMatches } from "./goToFileRanking";
import {
  GO_TO_FILE_VISIBLE_LIMIT,
  goToFileSearchState,
  goToFileStatusMessage,
} from "./goToFileSearchStatus";
import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

/** Matches the Files panel's search: one request per pause in typing. */
export const GO_TO_FILE_DEBOUNCE_MS = 200;
/** Keys that move cmdk's selection; after one, a refresh keeps the user's row. */
const SELECTION_KEYS = new Set(["ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

/**
 * The element to give focus back to when Go to File closes, read just before it
 * opens. Nothing inside a dialog, such as the ⌘K palette's input that is about
 * to be removed.
 */
export function captureGoToFileOpener(): HTMLElement | null {
  const focused = document.activeElement;
  if (!(focused instanceof HTMLElement) || focused === document.body) return null;
  return focused.closest('[role="dialog"], [role="alertdialog"]') ? null : focused;
}

function itemValue(path: string): string {
  return `file:${encodeURIComponent(path)}`;
}

function Highlighted({ text, range }: { text: string; range: MatchRange | null }) {
  if (!range) return text;
  return (
    <>
      {text.slice(0, range.start)}
      <mark className="bg-transparent font-semibold text-foreground">
        {text.slice(range.start, range.end)}
      </mark>
      {text.slice(range.end)}
    </>
  );
}

export interface GoToFileDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string;
  /** Opens the file in the workspace viewer, through the unsaved-changes guard. */
  onOpenFile: (path: string) => void;
  /** Where focus returns when the dialog closes without opening a file. */
  opener: HTMLElement | null;
}

export function GoToFileDialog({
  open,
  onOpenChange,
  conversationId,
  onOpenFile,
  opener,
}: GoToFileDialogProps) {
  // The file opens once the dialog has closed, so its focus trap can't pull
  // focus back from the viewer or an unsaved-changes prompt.
  const pathToOpen = useRef<string | null>(null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="top-1/3 translate-y-0 overflow-hidden rounded-xl! p-0"
        showCloseButton={false}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const path = pathToOpen.current;
          if (path !== null) {
            // Opening the file decides focus: the viewer, or an unsaved-changes prompt.
            pathToOpen.current = null;
            onOpenFile(path);
            return;
          }
          if (opener?.isConnected) opener.focus();
        }}
      >
        <DialogTitle className="sr-only">Go to File</DialogTitle>
        <DialogDescription className="sr-only">
          Open a file in this session's workspace by part of its name or path.
        </DialogDescription>
        {/* Mounted only while open, so a closed dialog runs no search and reopens empty. */}
        <GoToFileSearch
          conversationId={conversationId}
          onChoose={(path) => {
            pathToOpen.current = path;
            onOpenChange(false);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function GoToFileSearch({
  conversationId,
  onChoose,
}: {
  conversationId: string;
  onChoose: (path: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), GO_TO_FILE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const search = useWorkspaceFileSearch(conversationId, debouncedQuery);
  const serveable = useWorkspaceServeable(conversationId);
  const typed = query.trim();
  const state = goToFileSearchState({
    ...search,
    query,
    searchedQuery: debouncedQuery,
    serveable,
  });
  const settled = state.phase === "settled";
  const response = state.response;
  // Ranked once per answer and query, not on every selection move.
  const rows = useMemo(() => {
    if (!response) return [];
    // Rows from an earlier query stay only while they still match what's typed now.
    const entries = settled
      ? response.files
      : response.files.filter((entry) => stillMatches(entry.path, typed));
    return rankGoToFileResults(entries, typed);
  }, [response, settled, typed]);
  const visibleRows = rows.slice(0, GO_TO_FILE_VISIBLE_LIMIT);
  const status = goToFileStatusMessage(state, query, rows.length);

  const [selectedValue, setSelectedValue] = useState("");
  const userMovedSelection = useRef(false);
  const rowValues = visibleRows.map((row) => itemValue(row.path));
  const firstValue = rowValues[0] ?? "";
  const rowValuesKey = rowValues.join("\n");
  useEffect(() => {
    userMovedSelection.current = false;
  }, [debouncedQuery]);
  useEffect(() => {
    // The user's row survives a refresh or reorder; only its disappearance resets it.
    if (!userMovedSelection.current || !rowValuesKey.split("\n").includes(selectedValue)) {
      userMovedSelection.current = false;
      setSelectedValue(firstValue);
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [rowValuesKey, firstValue, debouncedQuery]);

  return (
    <Command
      shouldFilter={false}
      vimBindings={false}
      label="Go to File"
      value={selectedValue}
      onValueChange={setSelectedValue}
      onKeyDown={(event) => {
        if (SELECTION_KEYS.has(event.key)) userMovedSelection.current = true;
      }}
    >
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="Search files by name or path"
        data-testid="go-to-file-input"
      />
      <CommandList
        // Hovering a row selects it in cmdk, which counts as the user's pick.
        onPointerMove={(event) => {
          if ((event.target as Element).closest("[cmdk-item]")) {
            userMovedSelection.current = true;
          }
        }}
      >
        {visibleRows.map((row) => (
          <CommandItem
            key={row.path}
            value={itemValue(row.path)}
            onSelect={() => onChoose(row.path)}
          >
            <WorkspaceFileIcon path={row.path} className="size-4 shrink-0" />
            <span className="min-w-0 truncate">
              <Highlighted text={row.name} range={row.nameMatch} />
            </span>
            {row.folder && (
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                <Highlighted text={row.folder} range={row.folderMatch} />
              </span>
            )}
          </CommandItem>
        ))}
        {status && (
          <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
            {status}
          </p>
        )}
      </CommandList>
    </Command>
  );
}
