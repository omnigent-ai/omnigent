import { useEffect, useRef } from "react";
import { FileTextIcon, FolderIcon, PlusIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

interface FileMentionMenuProps {
  /** Directory currently being browsed ("" = workspace root). */
  currentDir: string;
  /** Index of the highlighted row (-1 = none). */
  activeIndex: number;
  /** Entries of the current directory (folders first), already filtered + capped. */
  entries: WorkspaceFile[];
  /**
   * True while the directory listing is still fetching (cold-boot root load or
   * a sub-directory's first load). Renders a loading row so "@" gives feedback
   * instead of appearing dead while ``entries`` is transiently empty.
   */
  loading?: boolean;
  /** Open (drill into) a folder by its workspace-relative path. */
  onOpenDir: (path: string) => void;
  /** Attach a file (isDir=false) or whole folder (isDir=true) as a unit. */
  onAttach: (path: string, isDir: boolean) => void;
}

/**
 * Floating drill-down file/folder browser shown when the user types ``@`` in
 * a native coding-agent session. Folders open (drill in) on click so nested
 * files are reachable; a file attaches on click, and a folder's ``+`` button
 * attaches the whole directory as a unit. Mirrors {@link SlashCommandMenu}'s
 * keep-the-active-row-visible behaviour. Exported for direct unit testing.
 */
export function FileMentionMenu({
  currentDir,
  activeIndex,
  entries,
  loading = false,
  onOpenDir,
  onAttach,
}: FileMentionMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  if (entries.length === 0 && !loading) return null;

  return (
    <div className="absolute bottom-full left-0 z-10 mb-2 flex items-end gap-2">
      <div className="w-80 max-w-[calc(100vw-2rem)] shrink-0 overflow-hidden rounded-xl border border-border bg-popover shadow-lg">
        <div className="flex items-center justify-between gap-2 px-2 pb-0.5 pt-1.5 text-[11px] font-medium text-muted-foreground">
          <span className="truncate">{currentDir ? `/${currentDir}` : "Workspace"}</span>
          <span className="shrink-0 text-[10px]">↵ open · ⇥ attach</span>
        </div>
        {entries.length === 0 && loading ? (
          <div className="px-3 py-2 text-[13px] text-muted-foreground">Loading…</div>
        ) : (
          <div ref={listRef} role="listbox" className="max-h-80 overflow-y-auto p-1">
            {entries.map((entry, i) => {
              const isDir = entry.type === "directory";
              return (
                <div
                  key={`${entry.type}:${entry.path}`}
                  role="option"
                  aria-selected={i === activeIndex}
                  data-testid={`file-mention-item-${i}`}
                  data-active={i === activeIndex ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-foreground",
                    i === activeIndex && "bg-accent",
                  )}
                >
                  <button
                    type="button"
                    // preventDefault keeps the textarea focused while clicking.
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => (isDir ? onOpenDir(entry.path) : onAttach(entry.path, false))}
                    className="flex min-w-0 flex-1 items-center gap-2 hover:text-foreground"
                    title={isDir ? `Open ${entry.name}` : `Attach ${entry.name}`}
                  >
                    {isDir ? (
                      <FolderIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                    ) : (
                      <FileTextIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                    )}
                    <span className="truncate">
                      {entry.name}
                      {isDir ? "/" : ""}
                    </span>
                  </button>
                  {isDir && (
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => onAttach(entry.path, true)}
                      className="flex shrink-0 items-center gap-0.5 rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
                      aria-label={`Attach whole folder ${entry.name}`}
                      title={`Attach whole folder ${entry.name}`}
                    >
                      <PlusIcon className="size-3" />
                      folder
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
