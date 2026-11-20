// The workspace Files tab's tree: the workspace root's listing and its uncommitted
// changes, read through the session's file queries, shown in the Files tree.

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

export function FilesPageTree({
  conversationId,
  activePath,
  sort,
  showHidden,
  onFileSelect,
}: FilesPageTreeProps) {
  const files = useWorkspaceAllFiles(conversationId);
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
  if (entries.length === 0) return <TreeMessage>No files in the workspace.</TreeMessage>;
  if (visibleEntries(entries, showHidden).length === 0) {
    return <TreeMessage>Every file here is hidden. Show hidden files to see them.</TreeMessage>;
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
      refreshToken={0}
      onFileSelect={onFileSelect}
      reveal={null}
      activeFile={activePath ?? null}
      label="Workspace files"
    />
  );
}
