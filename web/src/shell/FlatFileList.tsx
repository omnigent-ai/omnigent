import { FileIcon } from "lucide-react";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { gitStatusLabel, gitStatusLetter } from "./fileStatusUtils";
import { FileDownloadButton } from "./FileDownloadButton";
import { useCursorTooltip } from "./useCursorTooltip";

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

function normalizeSearchQuery(query: string): string {
  return query.trim().toLowerCase();
}

function FileListItem({
  file,
  isDeleted,
  onFileSelect,
  conversationId,
}: {
  file: WorkspaceChangedFile;
  isDeleted: boolean;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
}) {
  const { handlers, tooltip } = useCursorTooltip(file.path);
  const slash = file.path.lastIndexOf("/");
  const dir = slash > 0 ? file.path.slice(0, slash) : "";
  const hasDownload = !isDeleted && Boolean(conversationId);

  return (
    <li>
      <div
        className={cn(
          "group flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1",
          isDeleted ? "opacity-50" : "hover:bg-muted",
        )}
      >
        <button
          type="button"
          className={cn(
            "flex min-w-0 flex-1 items-baseline gap-1.5 text-left",
            isDeleted ? "cursor-default" : "cursor-pointer",
          )}
          onClick={() => !isDeleted && onFileSelect(file.path)}
          disabled={isDeleted}
        >
          <FileIcon className="size-3.5 shrink-0 self-center text-muted-foreground" />
          <span
            className={cn("truncate font-mono text-sm md:text-xs", isDeleted && "line-through")}
            {...handlers}
          >
            {file.name}
          </span>
          {dir && <span className="truncate text-muted-foreground text-[11px]">{dir}</span>}
        </button>
        {((file.lines_added ?? 0) !== 0 || (file.lines_removed ?? 0) !== 0) && (
          <span
            className="shrink-0 font-mono text-[10px]"
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
        <span className="relative flex shrink-0 items-center justify-center">
          <span
            className={cn(
              "rounded px-1 py-0.5 font-mono text-[10px]",
              hasDownload && "group-hover:invisible",
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
          {hasDownload && conversationId && (
            <span className="absolute inset-0 flex items-center justify-center">
              <FileDownloadButton conversationId={conversationId} path={file.path} />
            </span>
          )}
        </span>
      </div>
      {tooltip}
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
}) {
  if (isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (isError) {
    // Runner not connected. If it went offline after being up (host
    // restarted), guide the user to send a message to reconnect. If the
    // session just hasn't started, it isn't "asleep" — show the empty
    // state rather than alarm the user.
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-xs">No workspace changes yet</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">No workspace changes yet</p>;
  }
  const normalizedSearchQuery = normalizeSearchQuery(searchQuery);
  const visibleFiles = files.filter(
    (f) => showHidden || !f.path.split("/").some((seg) => seg.startsWith(".")),
  );
  const sorted = visibleFiles
    .filter(
      (f) =>
        normalizedSearchQuery.length === 0 ||
        f.name.toLowerCase().includes(normalizedSearchQuery) ||
        f.path.toLowerCase().includes(normalizedSearchQuery),
    )
    .sort(compareChangedFiles(sort));
  const hiddenCount = files.length - visibleFiles.length;
  if (visibleFiles.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
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
  if (sorted.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
        No changed files match "{searchQuery.trim()}"
      </p>
    );
  }
  return (
    <>
      {hiddenCount > 0 && (
        <p className="px-2 py-1 text-muted-foreground text-xs">
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
          {sorted.map((file) => {
            const isDeleted = file.status === "deleted";
            return (
              <FileListItem
                key={file.path}
                file={file}
                isDeleted={isDeleted}
                onFileSelect={onFileSelect}
                conversationId={conversationId}
              />
            );
          })}
        </ul>
      </TooltipProvider>
    </>
  );
}
