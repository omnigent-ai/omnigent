import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, GitBranch } from "lucide-react";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  ROW_ACTION_SIZE_CLASS,
  ROW_META_SLOT_CLASS,
  ROW_STATUS_SLOT_CLASS,
  formatBytes,
  gitStatusLabel,
  gitStatusLetter,
} from "./fileStatusUtils";
import { CopyPathButton } from "./CopyPathButton";
import { FileDownloadButton } from "./FileDownloadButton";
import { FileRowActions, ROW_MENU_SIZE_SLOT_CLASS, type FileRowInfo } from "./FileRowActions";
import { useCursorTooltip } from "./useCursorTooltip";
import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

export type { ChangedSort } from "@/lib/changedSort";
import type { ChangedSort } from "@/lib/changedSort";

function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/**
 * Minimal file shape the comparator needs. Both `WorkspaceChangedFile`
 * (Changed list) and `WorkspaceFile` (All tree) satisfy it, so the two views
 * order files identically for a given sort.
 */
export interface SortableFile {
  name: string;
  path: string;
  bytes: number | null;
  modified_at: number | null;
}

export function compareChangedFiles(sort: ChangedSort) {
  return (a: SortableFile, b: SortableFile): number => {
    if (sort === "recent") {
      const am = a.modified_at;
      const bm = b.modified_at;
      if (am === null && bm === null) return a.path.localeCompare(b.path);
      if (am === null) return 1;
      if (bm === null) return -1;
      if (am !== bm) return bm - am;
      return a.path.localeCompare(b.path);
    }
    if (sort === "size") {
      const ab = a.bytes;
      const bb = b.bytes;
      if (ab === null && bb === null) return a.path.localeCompare(b.path);
      if (ab === null) return 1;
      if (bb === null) return -1;
      if (ab !== bb) return bb - ab;
      return a.path.localeCompare(b.path);
    }
    if (sort === "type") {
      const ae = fileExtension(a.name);
      const be = fileExtension(b.name);
      if (ae !== be) return ae.localeCompare(be);
      return a.path.localeCompare(b.path);
    }
    return a.path.localeCompare(b.path);
  };
}

export function groupChangedFiles(files: WorkspaceChangedFile[], sort: ChangedSort) {
  const groups = new Map<string, WorkspaceChangedFile[]>();
  for (const file of files) {
    const root = file.repository_root ?? "";
    const group = groups.get(root) ?? [];
    group.push(file);
    groups.set(root, group);
  }
  return [...groups]
    .sort(([left], [right]) => (left === "" ? 1 : right === "" ? -1 : left.localeCompare(right)))
    .map(([root, groupFiles]) => ({ root, files: groupFiles.sort(compareChangedFiles(sort)) }));
}

function normalizeSearchQuery(query: string): string {
  return query.trim().toLowerCase();
}

function FileListItem({
  file,
  isDeleted,
  onFileSelect,
  conversationId,
  onOpenInfo,
}: {
  file: WorkspaceChangedFile;
  isDeleted: boolean;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  onOpenInfo?: (info: FileRowInfo, returnFocus: HTMLElement | null) => void;
}) {
  const { handlers, tooltip } = useCursorTooltip(file.path);
  const displayPath =
    file.repository_root && file.path.startsWith(`${file.repository_root}/`)
      ? file.path.slice(file.repository_root.length + 1)
      : file.path;
  const slash = displayPath.lastIndexOf("/");
  const dir = slash > 0 ? displayPath.slice(0, slash) : "";
  const hasDownload = !isDeleted && Boolean(conversationId);

  return (
    <li>
      <FileRowActions
        name={file.name}
        path={file.path}
        revealPath={isDeleted ? null : file.path}
        kind="file"
        conversationId={conversationId}
        downloadable={hasDownload}
        isDeleted={isDeleted}
        bytes={file.bytes}
        modifiedAt={file.modified_at}
        status={file.status}
        linesAdded={file.lines_added}
        linesRemoved={file.lines_removed}
        onOpenInfo={onOpenInfo ?? (() => {})}
      >
        {(moreActions, rowRef, primaryActionRef, actionsOpen) => (
          <div
            ref={rowRef}
            data-actions-open={actionsOpen}
            tabIndex={-1}
            className={cn(
              "group flex w-full min-w-0 select-none items-center gap-2 rounded-md py-0.5 pr-1 pl-2 [-webkit-touch-callout:none]",
              isDeleted ? "opacity-50" : "hover:bg-muted",
            )}
          >
            <button
              ref={primaryActionRef}
              type="button"
              className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-1.5 text-left"
              onClick={() => onFileSelect(file.path)}
              aria-label={file.path}
            >
              <WorkspaceFileIcon path={file.path} className="self-center" />
              <span className={cn("truncate text-ui", isDeleted && "line-through")} {...handlers}>
                {file.name}
              </span>
              {dir && <span className="truncate text-muted-foreground text-sm">{dir}</span>}
            </button>
            {/* Fixed-width and always rendered: a variable diffstat ("+7 −1" vs
            "+1204 −318") would otherwise shift the copy button and status
            letter to a different x on every row. */}
            <span className={cn("flex shrink-0 items-center justify-end", ROW_META_SLOT_CLASS)}>
              {((file.lines_added ?? 0) !== 0 || (file.lines_removed ?? 0) !== 0) && (
                <span
                  className="font-mono text-[10px]"
                  aria-label={[
                    file.lines_added !== null && `${file.lines_added} lines added`,
                    file.lines_removed !== null && `${file.lines_removed} removed`,
                  ]
                    .filter(Boolean)
                    .join(", ")}
                >
                  {file.lines_added !== null && (
                    <span className="text-green-600 dark:text-green-400">+{file.lines_added}</span>
                  )}
                  {file.lines_added !== null && file.lines_removed !== null && " "}
                  {file.lines_removed !== null && (
                    <span className="text-destructive">&minus;{file.lines_removed}</span>
                  )}
                </span>
              )}
            </span>
            <span
              className={cn("flex shrink-0 items-center justify-center", ROW_STATUS_SLOT_CLASS)}
            >
              <span
                className={cn(
                  "rounded px-1 py-0.5 font-mono text-[10px]",
                  isDeleted
                    ? "bg-destructive/10 text-destructive"
                    : file.status === "created"
                      ? "bg-green-500/10 text-green-600 dark:text-green-400"
                      : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
                )}
                title={gitStatusLabel(file.status)}
              >
                {gitStatusLetter(file.status)}
              </span>
            </span>
            {/* File size at rest, replaced by copy/download actions on hover — the
            same far-right slot used by the Files tree. */}
            <span
              className={cn(
                "relative flex shrink-0 items-center justify-end",
                ROW_META_SLOT_CLASS,
                ROW_MENU_SIZE_SLOT_CLASS,
                "pointer-coarse:justify-start",
              )}
            >
              {file.bytes !== null && !isDeleted && (
                <span
                  className={cn(
                    "mr-6 w-14 shrink-0 text-right text-muted-foreground text-sm group-hover:invisible group-has-[:focus-visible]:invisible",
                    actionsOpen && "invisible",
                  )}
                >
                  {formatBytes(file.bytes)}
                </span>
              )}
              <span className="absolute inset-0 flex items-center justify-end gap-1">
                {hasDownload && conversationId ? (
                  <FileDownloadButton conversationId={conversationId} path={file.path} />
                ) : (
                  <span className={cn("shrink-0", ROW_ACTION_SIZE_CLASS)} aria-hidden />
                )}
                <CopyPathButton path={file.path} revealOnHover />
                {moreActions}
              </span>
            </span>
          </div>
        )}
      </FileRowActions>
      {tooltip}
    </li>
  );
}

function RepositoryFileGroup({
  root,
  children,
  count,
}: {
  root: string;
  children: ReactNode;
  count: number;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const name = root || "Other files";
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <li>
      <div role="group" aria-label={name}>
        <button
          type="button"
          className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-ui hover:bg-muted"
          aria-expanded={!collapsed}
          aria-label={`${name}, ${count} changed files`}
          onClick={() => setCollapsed(!collapsed)}
        >
          <Chevron className="size-3.5 shrink-0" />
          {root && <GitBranch className="size-3.5 shrink-0 text-muted-foreground" />}
          <span className="min-w-0 flex-1 truncate">{name}</span>
          <span className="text-muted-foreground text-sm">{count}</span>
        </button>
        {!collapsed && <ul className="flex flex-col gap-0.5">{children}</ul>}
      </div>
    </li>
  );
}

export function FlatFileList({
  files,
  isLoading,
  isError,
  error,
  onFileSelect,
  showHidden,
  onShowHidden,
  searchQuery,
  sort,
  conversationId,
  runnerWentOffline = false,
  onOpenInfo,
}: {
  files: WorkspaceChangedFile[] | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  onFileSelect: (path: string) => void;
  showHidden: boolean;
  onShowHidden: () => void;
  searchQuery: string;
  sort: ChangedSort;
  /** Session ID used to fetch file content for downloads. */
  conversationId: string | undefined;
  /**
   * The runner went offline after being connected (session status
   * "failed", e.g. host restarted) — show the reconnect hint. When the
   * session simply hasn't started yet (a new session also 503s) this is
   * false and we fall through to the normal empty state instead.
   */
  runnerWentOffline?: boolean;
  onOpenInfo?: (info: FileRowInfo, returnFocus: HTMLElement | null) => void;
}) {
  if (isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-sm">Loading…</p>;
  }
  if (isError) {
    // Runner not connected. If it went offline after being up (host
    // restarted), guide the user to send a message to reconnect. If the
    // session just hasn't started, it isn't "asleep" — show the empty
    // state rather than alarm the user.
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-sm">No workspace changes yet</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-sm">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0) {
    return <p className="px-2 py-1 text-muted-foreground text-sm">No workspace changes yet</p>;
  }
  const normalizedSearchQuery = normalizeSearchQuery(searchQuery);
  const visibleFiles = files.filter(
    (f) => showHidden || !f.path.split("/").some((seg) => seg.startsWith(".")),
  );
  const matchingFiles = visibleFiles.filter(
    (f) =>
      normalizedSearchQuery.length === 0 ||
      f.name.toLowerCase().includes(normalizedSearchQuery) ||
      f.path.toLowerCase().includes(normalizedSearchQuery),
  );
  const groups = groupChangedFiles(matchingFiles, sort);
  const grouped = files.some((file) => file.repository_root);
  const hiddenCount = files.length - visibleFiles.length;
  if (visibleFiles.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-sm">
        All changes are in hidden files.{" "}
        <button
          type="button"
          className="cursor-pointer underline hover:text-foreground"
          onClick={onShowHidden}
        >
          Click to show
        </button>
      </p>
    );
  }
  if (matchingFiles.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-sm">
        No changed files match "{searchQuery.trim()}"
      </p>
    );
  }
  return (
    <>
      {hiddenCount > 0 && (
        <p className="px-2 py-1 text-muted-foreground text-sm">
          {hiddenCount} file{hiddenCount === 1 ? "" : "s"} hidden.{" "}
          <button
            type="button"
            className="cursor-pointer underline hover:text-foreground"
            onClick={onShowHidden}
          >
            Click to show
          </button>
        </p>
      )}
      <TooltipProvider>
        <ul className="flex flex-col gap-0.5">
          {groups.map(({ root, files: groupFiles }) => {
            const rows = groupFiles.map((file) => (
              <FileListItem
                key={file.path}
                file={file}
                isDeleted={file.status === "deleted"}
                onFileSelect={onFileSelect}
                conversationId={conversationId}
                onOpenInfo={onOpenInfo}
              />
            ));
            return grouped ? (
              <RepositoryFileGroup
                key={`${conversationId}/${root}`}
                root={root}
                count={groupFiles.length}
              >
                {rows}
              </RepositoryFileGroup>
            ) : (
              rows
            );
          })}
        </ul>
      </TooltipProvider>
    </>
  );
}
