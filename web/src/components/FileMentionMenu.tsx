import { useEffect, useRef } from "react";
import {
  ArrowLeftIcon,
  BotIcon,
  ChevronRightIcon,
  FileTextIcon,
  FolderIcon,
  MessageSquareIcon,
  MessagesSquareIcon,
  PlusIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { MobileMenuGlassSurface } from "./MobileMenuGlassSurface";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import type { SessionMenuView } from "@/hooks/useMentionBrowser";
import type { SessionMention } from "@/lib/composerMentions";

interface FileMentionMenuProps {
  /** Directory currently being browsed ("" = workspace root). */
  currentDir: string;
  /** Typed text after ``@``; its first match in each session title is tinted. */
  query?: string;
  /** Typed name segment within ``currentDir``; tinted in file and folder names. */
  fileQuery?: string;
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
  /** Session rows, the bare-``@`` category row, and the open-list state. */
  sessionMenu?: SessionMenuView;
  /** Tag a session. Required when ``sessionMenu`` has rows. */
  onAttachSession?: (session: SessionMention) => void;
}

const ROW_CLASS =
  "flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-ui text-foreground";
const ACTIVE_ROW_CLASS = "bg-muted dark:bg-muted/50";
const ROW_ICON_CLASS = "size-3.5 shrink-0 text-slate-500 dark:text-slate-400";

/** ``text`` with the first case-insensitive match of ``query`` tinted like a mention token. */
export function MatchHighlight({ text, query }: { text: string; query: string }) {
  const at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at < 0) return text;
  const end = at + query.length;
  return (
    <>
      {text.slice(0, at)}
      <span data-testid="mention-match" className="text-brand-accent">
        {text.slice(at, end)}
      </span>
      {text.slice(end)}
    </>
  );
}

/**
 * Floating ``@`` menu. A bare ``@`` shows one "Sessions" row; opening it lists
 * a "Subagents" row and then the other sessions, and Back or Esc goes up one
 * level. Typed text lists matching sessions inline. Below them, in native coding-agent sessions, sits a
 * drill-down file/folder browser. Row indices run top to bottom across all
 * sections. Folders open (drill in) on click so nested
 * files are reachable; a file attaches on click, and a folder's ``+`` button
 * attaches the whole directory as a unit. Mirrors {@link SlashCommandMenu}'s
 * keep-the-active-row-visible behaviour. Exported for direct unit testing.
 */
export function FileMentionMenu({
  currentDir,
  query = "",
  fileQuery = "",
  activeIndex,
  entries,
  loading = false,
  onOpenDir,
  onAttach,
  sessionMenu,
  onAttachSession,
}: FileMentionMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  const sessions = sessionMenu?.rows ?? [];
  const category = sessionMenu?.category ?? null;
  const level = sessionMenu?.level ?? null;
  if (entries.length === 0 && sessions.length === 0 && !category && !level && !loading) return null;
  const sessionOffset = category ? 1 : 0;
  const fileOffset = sessionOffset + sessions.length;
  const showFiles = level === null && (entries.length > 0 || loading);

  return (
    <div className="absolute bottom-full left-0 z-30 mb-2 flex items-end gap-2">
      <div
        data-testid="file-mention-menu"
        className="mobile-menu-glass max-md:relative w-80 max-w-[calc(100vw-2rem)] shrink-0 overflow-hidden rounded-[12px] border border-border bg-popover p-2 shadow-menu dark:backdrop-blur-xl dark:backdrop-saturate-150"
      >
        <MobileMenuGlassSurface />
        <div ref={listRef} role="listbox" className="max-h-80 overflow-y-auto">
          {level && (
            <button
              type="button"
              data-testid="session-mention-back"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => sessionMenu?.goBack()}
              className={cn(ROW_CLASS, "text-muted-foreground hover:text-foreground")}
            >
              <ArrowLeftIcon className="size-3.5 shrink-0" />
              <span className="flex-1">Back</span>
              <kbd className="shrink-0 rounded border border-border px-1 text-[10px]">Esc</kbd>
            </button>
          )}
          {category && (
            <button
              type="button"
              role="option"
              aria-selected={activeIndex === 0}
              data-testid={`session-mention-category-${category}`}
              data-active={activeIndex === 0 ? "true" : undefined}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => sessionMenu?.openCategory()}
              className={cn(ROW_CLASS, activeIndex === 0 && ACTIVE_ROW_CLASS)}
            >
              {category === "subagents" ? (
                <BotIcon className={ROW_ICON_CLASS} />
              ) : (
                <MessagesSquareIcon className={ROW_ICON_CLASS} />
              )}
              <span className="flex-1">{category === "subagents" ? "Subagents" : "Sessions"}</span>
              <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground" />
            </button>
          )}
          {sessions.length > 0 && (
            <>
              {level === null && (
                <div className="px-1.5 py-1 text-sm font-medium text-muted-foreground">
                  Sessions
                </div>
              )}
              {sessions.map((session, sessionIndex) => {
                const i = sessionOffset + sessionIndex;
                return (
                  <button
                    key={`session:${session.id}`}
                    type="button"
                    role="option"
                    aria-selected={i === activeIndex}
                    data-testid={`session-mention-item-${sessionIndex}`}
                    data-active={i === activeIndex ? "true" : undefined}
                    // preventDefault keeps the textarea focused while clicking.
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onAttachSession?.(session)}
                    title={`Tag ${session.isSubagent ? "subagent" : "session"} ${session.title}`}
                    className={cn(ROW_CLASS, i === activeIndex && ACTIVE_ROW_CLASS)}
                  >
                    {session.isSubagent ? (
                      <BotIcon className={ROW_ICON_CLASS} />
                    ) : (
                      <MessageSquareIcon className={ROW_ICON_CLASS} />
                    )}
                    <span className="min-w-0 flex-1 truncate">
                      <MatchHighlight text={session.title} query={query} />
                    </span>
                    {session.isSubagent && (
                      <span className="shrink-0 text-[10px] text-muted-foreground">subagent</span>
                    )}
                  </button>
                );
              })}
            </>
          )}
          {level && sessions.length === 0 && !category && (
            <div className="px-1.5 py-1 text-ui text-muted-foreground">
              {level === "subagents" ? "No matching subagents" : "No matching sessions"}
            </div>
          )}
          {showFiles && (
            <div className="flex items-center justify-between gap-2 px-1.5 py-1 text-sm font-medium text-muted-foreground">
              <span className="truncate">{currentDir ? `/${currentDir}` : "Workspace"}</span>
              <span className="shrink-0 text-[10px]">↵ open · ⇥ attach</span>
            </div>
          )}
          {!showFiles ? null : entries.length === 0 && loading ? (
            <div className="px-1.5 py-1 text-ui text-muted-foreground">Loading…</div>
          ) : (
            entries.map((entry, fileIndex) => {
              const i = fileOffset + fileIndex;
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
                      <MatchHighlight text={entry.name} query={fileQuery} />
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
