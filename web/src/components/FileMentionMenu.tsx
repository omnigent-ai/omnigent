import { useEffect, useRef } from "react";
import { BotIcon, FileTextIcon, FolderIcon, MessageSquareIcon, PlusIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { MobileMenuGlassSurface } from "./MobileMenuGlassSurface";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { NO_SESSION_MENTIONS, type SessionMention } from "@/lib/composerMentions";

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
  /** Matching sessions, rendered above the files and indexed first. */
  sessions?: readonly SessionMention[];
  /** Tag a session. Required when ``sessions`` is non-empty. */
  onAttachSession?: (session: SessionMention) => void;
}

/**
 * Floating ``@`` menu: other sessions (including this session's subagents)
 * first, then — in native coding-agent sessions — a drill-down file/folder
 * browser. Row indices run across both sections, sessions first. Folders open (drill in) on click so nested
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
  sessions = NO_SESSION_MENTIONS,
  onAttachSession,
}: FileMentionMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  if (entries.length === 0 && sessions.length === 0 && !loading) return null;
  const showFiles = entries.length > 0 || loading;

  return (
    <div className="absolute bottom-full left-0 z-30 mb-2 flex items-end gap-2">
      <div
        data-testid="file-mention-menu"
        className="mobile-menu-glass max-md:relative w-80 max-w-[calc(100vw-2rem)] shrink-0 overflow-hidden rounded-[12px] border border-border bg-popover p-2 shadow-menu dark:backdrop-blur-xl dark:backdrop-saturate-150"
      >
        <MobileMenuGlassSurface />
        <div ref={listRef} role="listbox" className="max-h-80 overflow-y-auto">
          {sessions.length > 0 && (
            <>
              <div className="px-1.5 py-1 text-sm font-medium text-muted-foreground">Sessions</div>
              {sessions.map((session, i) => (
                <button
                  key={`session:${session.id}`}
                  type="button"
                  role="option"
                  aria-selected={i === activeIndex}
                  data-testid={`session-mention-item-${i}`}
                  data-active={i === activeIndex ? "true" : undefined}
                  // preventDefault keeps the textarea focused while clicking.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => onAttachSession?.(session)}
                  title={`Tag ${session.isSubagent ? "subagent" : "session"} ${session.title}`}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-ui text-foreground",
                    i === activeIndex && "bg-muted dark:bg-muted/50",
                  )}
                >
                  {session.isSubagent ? (
                    <BotIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                  ) : (
                    <MessageSquareIcon className="size-3.5 shrink-0 text-slate-500 dark:text-slate-400" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{session.title}</span>
                  {session.isSubagent && (
                    <span className="shrink-0 text-[10px] text-muted-foreground">subagent</span>
                  )}
                </button>
              ))}
            </>
          )}
          {showFiles && (
            <div className="flex items-center justify-between gap-2 px-1.5 py-1 text-sm font-medium text-muted-foreground">
              <span className="truncate">{currentDir ? `/${currentDir}` : "Workspace"}</span>
              <span className="shrink-0 text-[10px]">↵ open · ⇥ attach</span>
            </div>
          )}
          {entries.length === 0 && loading ? (
            <div className="px-1.5 py-1 text-ui text-muted-foreground">Loading…</div>
          ) : (
            entries.map((entry, fileIndex) => {
              const i = sessions.length + fileIndex;
              const isDir = entry.type === "directory";
              return (
                <div
                  key={`${entry.type}:${entry.path}`}
                  role="option"
                  aria-selected={i === activeIndex}
                  data-testid={`file-mention-item-${fileIndex}`}
                  data-active={i === activeIndex ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-ui text-foreground",
                    i === activeIndex && "bg-muted dark:bg-muted/50",
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
                      className="flex shrink-0 items-center gap-0.5 rounded-md border border-border px-1.5 py-0.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
                      aria-label={`Attach whole folder ${entry.name}`}
                      title={`Attach whole folder ${entry.name}`}
                    >
                      <PlusIcon className="size-3" />
                      folder
                    </button>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
