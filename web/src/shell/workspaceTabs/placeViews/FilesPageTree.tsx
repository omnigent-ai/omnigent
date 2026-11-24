// The workspace Files tab's tree: the workspace root's listing and its uncommitted
// changes, read through the session's file queries, shown in the Files tree.

import { Button } from "@/components/ui/button";
import type { ChangedSort } from "@/lib/changedSort";
import {
  RunnerOfflineError,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
} from "@/hooks/useWorkspaceChangedFiles";
import { visibleEntries } from "@/shell/fileTree/filesTreeListings";
import { FilesTree } from "@/shell/fileTree/FilesTree";

export interface FilesPageTreeProps {
  conversationId: string;
  /** The file the page shows, which the tree opens down to. */
  activePath: string | undefined;
  sort: ChangedSort;
  showHidden: boolean;
  /** Shows hidden files, for a workspace whose every file is hidden. */
  onShowHidden?: () => void;
  /** List what Git ignores, marked as ignored; otherwise leave it out. */
  showIgnored: boolean;
  /** Shows ignored files, for a workspace where Git ignores every file. */
  onShowIgnored?: () => void;
  /** `newTab` for a ⌘-click (Ctrl-click off macOS) or a middle click, which want the file in its own tab. */
  onFileSelect: (path: string, options?: { newTab: true }) => void;
}

function TreeMessage({ children, alert = false }: { children: string; alert?: boolean }) {
  return (
    <p
      role={alert ? "alert" : "status"}
      className={
        alert ? "px-3 py-2 text-ui text-destructive" : "px-3 py-2 text-ui text-muted-foreground"
      }
    >
      {children}
    </p>
  );
}

/** Why a listing shows nothing: everything left is hidden, or Git ignores the rest. */
function hiddenListingMessage(listed: number, ignoredHidden: boolean): string {
  if (listed === 0) return "No files here, apart from any that Git ignores.";
  return ignoredHidden
    ? "Every file here is hidden or ignored by Git."
    : "Every file here is hidden.";
}

export function FilesPageTree({
  conversationId,
  activePath,
  sort,
  showHidden,
  onShowHidden,
  showIgnored,
  onShowIgnored,
  onFileSelect,
}: FilesPageTreeProps) {
  const files = useWorkspaceAllFiles(conversationId, { hideIgnored: !showIgnored });
  const changes = useWorkspaceChangedFiles(conversationId);
  if (files.isPending) return <TreeMessage>Loading files…</TreeMessage>;
  if (files.isError) {
    return files.error instanceof RunnerOfflineError ? (
      <TreeMessage>The session’s machine is offline. Send a message to reconnect it.</TreeMessage>
    ) : (
      <TreeMessage alert>{`Couldn’t list the files: ${files.error.message}`}</TreeMessage>
    );
  }
  if (!files.data.available) return <TreeMessage>This session has no workspace files.</TreeMessage>;
  const entries = files.data.data;
  // Only a host that left out what Git ignores can be hiding more than this listing shows.
  const ignoredHidden = files.data.ignoredHidden === true;
  const hidden = visibleEntries(entries, showHidden).length === 0;
  if (entries.length === 0 && !ignoredHidden) {
    return <TreeMessage>No files in the workspace.</TreeMessage>;
  }
  if (hidden) {
    return (
      <div className="flex flex-col items-start gap-2 pb-2">
        <TreeMessage>{hiddenListingMessage(entries.length, ignoredHidden)}</TreeMessage>
        <div className="mx-3 flex flex-wrap gap-2">
          {entries.length > 0 && onShowHidden ? (
            <Button type="button" variant="outline" size="sm" onClick={onShowHidden}>
              Show hidden files
            </Button>
          ) : null}
          {ignoredHidden && onShowIgnored ? (
            <Button type="button" variant="outline" size="sm" onClick={onShowIgnored}>
              Show git-ignored files
            </Button>
          ) : null}
        </div>
      </div>
    );
  }
  return (
    <FilesTree
      key={conversationId}
      conversationId={conversationId}
      browseLocation=""
      rootEntries={entries}
      changedFiles={changes.data?.available ? changes.data.data : []}
      sort={sort}
      showHidden={showHidden}
      showIgnored={showIgnored}
      refreshToken={0}
      onFileSelect={onFileSelect}
      reveal={null}
      activeFile={activePath ?? null}
      label="Workspace files"
    />
  );
}
