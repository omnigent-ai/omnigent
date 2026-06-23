import { FileIcon } from "lucide-react";
import { useState } from "react";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { formatBytes, gitStatusLabel, gitStatusLetter } from "./fileStatusUtils";
import { FileDownloadButton } from "./FileDownloadButton";
import { useCursorTooltip } from "./useCursorTooltip";

export type ChangedSort = "alpha" | "recent";
type ChangedStageFilter = "all" | "staged" | "unstaged";

/**
 * Comparator for the changed-files list. Shared between the displayed list
 * (`FlatFileList`) and the prev/next navigation in `FileViewer` so the two
 * orderings never diverge — a file shown 1st in the list must also be 1st
 * when stepping through with the arrows.
 */
export function compareChangedFiles(sort: ChangedSort) {
  return (a: WorkspaceChangedFile, b: WorkspaceChangedFile): number => {
    if (sort === "recent") {
      // Most recently edited first. Files without a timestamp sink to
      // the bottom; ties fall back to alphabetical for stable order.
      const am = a.modified_at;
      const bm = b.modified_at;
      if (am === null && bm === null) return a.path.localeCompare(b.path);
      if (am === null) return 1;
      if (bm === null) return -1;
      if (am !== bm) return bm - am;
      return a.path.localeCompare(b.path);
    }
    return a.path.localeCompare(b.path);
  };
}

function normalizeSearchQuery(query: string): string {
  return query.trim().toLowerCase();
}

const STAGE_FILTERS: { value: ChangedStageFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "staged", label: "Staged" },
  { value: "unstaged", label: "Unstaged" },
];

function stageChangeLabel(filter: ChangedStageFilter): string {
  if (filter === "staged") return "staged changes";
  if (filter === "unstaged") return "unstaged changes";
  return "changes";
}

function stageFileLabel(filter: ChangedStageFilter): string {
  if (filter === "staged") return "staged files";
  if (filter === "unstaged") return "unstaged files";
  return "changed files";
}

function ChangedStageFilterControl({
  value,
  onChange,
}: {
  value: ChangedStageFilter;
  onChange: (next: ChangedStageFilter) => void;
}) {
  const pill =
    "flex cursor-pointer items-center rounded-full px-2.5 py-[2px] text-[12px] font-medium leading-5 transition-colors";
  const activePill =
    "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground";
  const idlePill = "text-muted-foreground hover:text-foreground";
  return (
    <div
      role="radiogroup"
      aria-label="Change stage"
      className="mb-1 flex items-center gap-1 px-0.5"
    >
      {STAGE_FILTERS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          aria-label={option.label}
          onClick={() => onChange(option.value)}
          className={cn(pill, value === option.value ? activePill : idlePill)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function supportsStageFiltering(files: WorkspaceChangedFile[]): boolean {
  return files.some(
    (file) => typeof file.staged === "boolean" || typeof file.unstaged === "boolean",
  );
}

function matchesStageFilter(file: WorkspaceChangedFile, filter: ChangedStageFilter): boolean {
  if (filter === "staged") return file.staged === true;
  if (filter === "unstaged") return file.unstaged === true;
  return true;
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

  return (
    <li>
      <div
        className={cn(
          "group flex w-full min-w-0 items-center gap-1.5 rounded-md px-2 py-1",
          isDeleted ? "opacity-50" : "hover:bg-muted",
        )}
      >
        <button
          type="button"
          className={cn(
            "flex min-w-0 flex-1 items-center gap-1.5 text-left",
            isDeleted ? "cursor-default" : "cursor-pointer",
          )}
          onClick={() => !isDeleted && onFileSelect(file.path)}
          disabled={isDeleted}
        >
          <span
            className={cn(
              "shrink-0 rounded px-1 py-0.5 font-mono text-[10px]",
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
          <FileIcon className="size-3.5 shrink-0 text-muted-foreground" />
          <span
            className={cn(
              "min-w-0 flex-1 truncate text-left font-mono text-sm md:text-xs [direction:rtl]",
              isDeleted && "line-through",
            )}
            {...handlers}
          >
            <bdi>{file.path}</bdi>
          </span>
          {file.bytes !== null && !isDeleted && (
            <span className="shrink-0 text-muted-foreground text-[10px]">
              {formatBytes(file.bytes)}
            </span>
          )}
        </button>
        {!isDeleted && conversationId && (
          <FileDownloadButton conversationId={conversationId} path={file.path} />
        )}
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
  const [stageFilter, setStageFilter] = useState<ChangedStageFilter>("all");

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
  const canFilterByStage = supportsStageFiltering(files);
  const stageFilteredFiles = canFilterByStage
    ? files.filter((file) => matchesStageFilter(file, stageFilter))
    : files;
  const normalizedSearchQuery = normalizeSearchQuery(searchQuery);
  const visibleFiles = stageFilteredFiles.filter(
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
  const hiddenCount = stageFilteredFiles.length - visibleFiles.length;
  const stageControl = canFilterByStage ? (
    <ChangedStageFilterControl value={stageFilter} onChange={setStageFilter} />
  ) : null;
  const stageChange = stageChangeLabel(stageFilter);
  const stageFiles = stageFileLabel(stageFilter);

  if (stageFilteredFiles.length === 0) {
    return (
      <>
        {stageControl}
        <p className="px-2 py-1 text-muted-foreground text-xs">No {stageChange}</p>
      </>
    );
  }
  if (visibleFiles.length === 0) {
    return (
      <>
        {stageControl}
        <p className="px-2 py-1 text-muted-foreground text-xs">
          All {stageChange} are in hidden files.{" "}
          <button
            type="button"
            className="cursor-pointer underline hover:text-foreground"
            onClick={onShowHidden}
          >
            Click to show
          </button>
        </p>
      </>
    );
  }
  if (sorted.length === 0) {
    return (
      <>
        {stageControl}
        <p className="px-2 py-1 text-muted-foreground text-xs">
          No {stageFiles} match "{searchQuery.trim()}"
        </p>
      </>
    );
  }
  return (
    <>
      {stageControl}
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
