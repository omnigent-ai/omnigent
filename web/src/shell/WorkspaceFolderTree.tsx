// One workspace folder as a keyboard-navigable tree that loads subfolders as
// they expand, with one entry, a file or a folder, revealed and selected.

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { FILE_TREE_ROOT_SELECTOR, FileTree, useFileTree } from "@/components/file-tree/vendored";
import { FILE_TREE_THEME, WORKSPACE_FILE_TREE_CLASS } from "@/components/file-tree/fileTreeStyle";
import { Button } from "@/components/ui/button";
import { useDelayedFlag } from "@/hooks/useDelayedFlag";
import {
  FOLDER_PICKER_ENTRY_LIMIT,
  type WorkspaceFile,
  useWorkspaceServeable,
  workspaceFolderListingQuery,
} from "@/hooks/useWorkspaceChangedFiles";
import { folderTreePath, revealedFolders } from "./breadcrumbPaths";
import { visibleTreeRow } from "./folderTreeSync";
import { SingleSelection } from "./fileTree/treeSelection";
import { initialFolderTree, useLazyFolderTree } from "./fileTree/useLazyFolderTree";
import { useTreeRowInput } from "./fileTree/useTreeRowInput";

interface WorkspaceFolderTreeProps {
  conversationId: string;
  /** The folder the tree shows, as a browse location: `""` for the workspace root. */
  folder: string;
  /** The entry to reveal and select, a file or a folder, relative to `folder`. */
  revealPath: string;
  /** Opens a file chosen in the tree; the path is relative to `folder`. */
  onOpenFile: (pathWithinFolder: string) => void;
  /** Names the tree for assistive technology, e.g. "Files in src". */
  label: string;
  dark: boolean;
}

/** Loads the folders down to the revealed entry, then shows the tree. */
export function WorkspaceFolderTree(props: WorkspaceFolderTreeProps) {
  const { conversationId, folder, revealPath } = props;
  const serveable = useWorkspaceServeable(conversationId);
  const folders = useMemo(() => revealedFolders(revealPath), [revealPath]);
  const listings = useQueries({
    queries: folders.map((subfolder) => ({
      ...workspaceFolderListingQuery(conversationId, subfolder, folder),
      enabled: serveable !== false,
    })),
  });
  // A failed refetch keeps its data, so only a listing never loaded fails the tree.
  const failed = listings.filter((listing) => listing.isError && listing.data === undefined);
  const showLoading = useDelayedFlag(listings.some((listing) => listing.isPending));

  if (serveable === false) {
    return <TreeMessage>The workspace is offline. Files will show when it reconnects.</TreeMessage>;
  }
  if (failed.length > 0) {
    return (
      <TreeMessage>
        <span>Couldn’t load this folder.</span>
        <Button
          type="button"
          variant="outline"
          size="xs"
          onClick={() => failed.forEach((listing) => void listing.refetch())}
        >
          Retry
        </Button>
      </TreeMessage>
    );
  }
  if (listings.some((listing) => listing.data === undefined)) {
    return <TreeMessage>{showLoading ? "Loading folder…" : null}</TreeMessage>;
  }
  return (
    <LoadedFolderTree
      key={`${conversationId}\0${folder}\0${revealPath}`}
      {...props}
      folders={folders}
      listings={listings.map((listing) => listing.data?.entries ?? [])}
    />
  );
}

function TreeMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 px-3 py-6 text-ui text-muted-foreground">
      {children}
    </div>
  );
}

function LoadedFolderTree({
  conversationId,
  folder,
  revealPath,
  onOpenFile,
  label,
  dark,
  folders,
  listings,
}: WorkspaceFolderTreeProps & { folders: string[]; listings: WorkspaceFile[][] }) {
  // The model starts from the first listings; later changes are applied to it in place.
  const [initial] = useState(() =>
    initialFolderTree(listings.map((entries, index) => ({ folder: folders[index], entries }))),
  );
  const initialRow = visibleTreeRow(initial.present, revealPath);

  // A file opens from the click, Enter or Space that chose it; selection changes open nothing.
  const selectionRef = useRef<SingleSelection | null>(null);
  const { model } = useFileTree({
    paths: [...initial.present],
    flattenEmptyDirectories: true,
    initialExpansion: "closed",
    initialExpandedPaths: folders.slice(1).map(folderTreePath),
    initialSelectedPaths: initialRow ? [initialRow] : undefined,
    onSelectionChange: () => selectionRef.current?.normalize(),
  });
  selectionRef.current ??= new SingleSelection(model);
  const selection = selectionRef.current;
  const treeContainer = useRef<HTMLDivElement>(null);
  useTreeRowInput(treeContainer, selection, (path) => {
    if (path.endsWith("/") || path === revealPath) return;
    onOpenFile(path);
  });

  // Every listed folder stays subscribed, so a refetch or invalidation reaches the tree.
  const [expandedFolders, setExpandedFolders] = useState<string[]>([]);
  const watchedFolders = useMemo(
    () => [...folders, ...expandedFolders],
    [folders, expandedFolders],
  );
  const watched = useQueries({
    queries: watchedFolders.map((subfolder) =>
      workspaceFolderListingQuery(conversationId, subfolder, folder),
    ),
  });
  // Changes whenever any watched listing receives data, refetches included.
  const listingVersion = watched.map((listing) => listing.dataUpdatedAt).join(",");
  const watchedListings = watched.flatMap(({ data }, index) =>
    data ? [{ folder: watchedFolders[index], entries: data.entries }] : [],
  );
  // A folder past the picker's limit lists its first entries and says so.
  const cutOffFolder = watchedFolders.find((_, index) => watched[index].data?.hasMore);
  const { wantedFolders, isEmpty, present } = useLazyFolderTree(
    model,
    initial,
    watchedListings,
    listingVersion,
    folders,
  );
  useEffect(() => {
    setExpandedFolders((current) =>
      current.length === wantedFolders.length &&
      current.every((path, index) => path === wantedFolders[index])
        ? current
        : wantedFolders,
    );
  }, [wantedFolders]);

  // An expanded folder's first listing reports its progress and failure; a
  // refresh of a folder already shown stays quiet.
  const expandedStart = folders.length;
  const firstLoads = watched
    .slice(expandedStart)
    .map((listing, index) => ({ listing, folder: expandedFolders[index] }))
    .filter(({ listing }) => listing.data === undefined);
  const failedLoad = firstLoads.find(({ listing }) => listing.isError);
  const pendingLoad = firstLoads.find(({ listing }) => listing.isPending);

  // A refresh can fold the revealed folder into a chain row, or unfold it. The
  // tree then moves focus on its own, so follow the row until the reader acts.
  const revealedRow = visibleTreeRow(present, revealPath);
  const markedRow = useRef(initialRow);
  const readerActed = useRef(false);
  const [focusTarget, setFocusTarget] = useState(initialRow);
  useEffect(() => {
    if (revealedRow === markedRow.current) return;
    markedRow.current = revealedRow;
    if (!revealedRow || readerActed.current) return;
    selection.selectWithoutFocus(revealedRow);
    setFocusTarget(revealedRow);
  }, [selection, revealedRow]);

  // Land keyboard focus on the revealed entry, or the first row, once the
  // virtualized rows have rendered.
  useEffect(() => {
    let frame = 0;
    let attempts = 0;
    const focusRow = () => {
      // Leave focus alone once the reader has moved it or used the tree.
      if (readerActed.current) return;
      const active = document.activeElement;
      const focusIsAround =
        !active ||
        active === document.body ||
        active.contains(treeContainer.current) ||
        treeContainer.current?.contains(active) === true;
      if (!focusIsAround) return;
      const target = focusTarget ?? model.getVisibleRows(0, 1)[0]?.path;
      if (!target) return;
      if (attempts === 0) {
        model.scrollToPath(target, { offset: "center" });
        model.focusPath(target);
      }
      const row = treeContainer.current
        ?.querySelector(FILE_TREE_ROOT_SELECTOR)
        ?.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]');
      if (row) {
        row.focus({ preventScroll: true });
        return;
      }
      if (++attempts < 30) frame = requestAnimationFrame(focusRow);
    };
    frame = requestAnimationFrame(focusRow);
    return () => cancelAnimationFrame(frame);
  }, [model, focusTarget]);

  return (
    // The tree stays mounted while its folder is empty, so its listeners are there once files arrive.
    <div
      ref={treeContainer}
      className="flex min-h-0 flex-1 flex-col"
      onKeyDownCapture={() => (readerActed.current = true)}
      onPointerDownCapture={() => (readerActed.current = true)}
    >
      {isEmpty && <TreeMessage>This folder is empty.</TreeMessage>}
      <FileTree
        model={model}
        aria-label={label}
        className={WORKSPACE_FILE_TREE_CLASS}
        style={{
          ...FILE_TREE_THEME,
          flex: 1,
          minHeight: 0,
          colorScheme: dark ? "dark" : "light",
          ...(isEmpty ? { display: "none" } : {}),
        }}
      />
      {!isEmpty && !(failedLoad ?? pendingLoad) && cutOffFolder !== undefined && (
        <div
          role="status"
          className="shrink-0 truncate border-t border-border px-3 py-1.5 text-ui text-muted-foreground"
        >
          {cutOffMessage(cutOffFolder === "" ? folder : cutOffFolder, revealPath, present)}
        </div>
      )}
      {!isEmpty && (failedLoad ?? pendingLoad) && (
        <div
          role="status"
          className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-3 py-1.5 text-ui text-muted-foreground"
        >
          <span className="truncate">
            {failedLoad
              ? `Couldn’t load ${baseName(failedLoad.folder)}.`
              : `Loading ${baseName(pendingLoad?.folder ?? "")}…`}
          </span>
          {failedLoad && (
            <Button
              type="button"
              variant="outline"
              size="xs"
              onClick={() => void failedLoad.listing.refetch()}
            >
              Retry
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** Why a folder's list stops short, naming the revealed entry when it lies past the limit. */
function cutOffMessage(cutOff: string, revealPath: string, present: ReadonlySet<string>): string {
  const limit = FOLDER_PICKER_ENTRY_LIMIT.toLocaleString("en-US");
  const name = baseName(cutOff) || "this folder";
  const missing = visibleTreeRow(present, revealPath) === undefined ? baseName(revealPath) : null;
  return missing
    ? `Showing the first ${limit} entries of ${name}, which don’t include ${missing}.`
    : `Showing the first ${limit} entries of ${name}.`;
}

function baseName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}
