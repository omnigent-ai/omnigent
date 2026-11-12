// ⌘K palette: Sessions (recent, or title matches), In messages (content matches), Actions.
// Sessions are searched server-side (one loaded page can't find older ones), so
// cmdk filtering is off and the static actions are filtered here.

import type React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { XIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { CompactShortcutKeys } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { type ActionCommand, filterAppCommands, useAppCommands } from "./appCommands";
import { type PaletteSession, usePaletteSessionSearch } from "./usePaletteSessionSearch";

export interface CommandPaletteProps {
  sessionsOnly?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Flip the left (Conversations) sidebar — owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar — owned by AppShell. */
  onToggleRightSidebar: () => void;
  /** False on grid routes, where ⌘⌥] belongs to the grid's own workspace. */
  rightSidebarShortcutEnabled?: boolean;
}

/** Split `text` on case-insensitive occurrences of `query`, bolding the matches
    so a search hit is visible in the title / content snippet. Returns the raw
    text unchanged when the query is empty or doesn't occur (e.g. the snippet
    matched a stemmed form). */
function HighlightedText({ text, query }: { text: string; query: string }): React.ReactNode {
  const q = query.trim();
  if (!q) return text;
  // Escape regex metacharacters so a query like "a.b" matches literally.
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  const lower = q.toLowerCase();
  // Non-matches stay raw strings (React needs no key for those); each match is
  // keyed by its running offset so repeated terms get stable, unique keys
  // without leaning on the bare array index.
  let offset = 0;
  return parts.map((part) => {
    const at = offset;
    offset += part.length;
    return part.toLowerCase() === lower ? (
      <mark key={at} className="bg-transparent font-semibold text-foreground">
        {part}
      </mark>
    ) : (
      part
    );
  });
}

/** Keys that move cmdk's selection; see `userMovedSelection`. */
const SELECTION_KEYS = new Set(["ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

export function CommandPalette({
  sessionsOnly = false,
  open,
  onOpenChange,
  onToggleLeftSidebar,
  onToggleRightSidebar,
  rightSidebarShortcutEnabled = true,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const {
    query,
    setQuery,
    debouncedQuery,
    searching,
    sessions,
    visibleSessions,
    contentSessions,
    titleSearch,
    contentSearch,
    isFetching,
    loadError,
    loadMoreError,
    searchFetching,
    retrying,
    retryLoad,
    olderSessionsAvailable,
    searchOlderSessions,
    rememberSearchRow,
  } = usePaletteSessionSearch({ open, sessionsOnly });

  const close = (): void => onOpenChange(false);

  const actions = useAppCommands({
    onToggleLeftSidebar,
    onToggleRightSidebar,
    rightSidebarShortcutEnabled,
  });

  const filteredActions = useMemo(
    () => (sessionsOnly ? [] : filterAppCommands(actions, query)),
    [actions, query, sessionsOnly],
  );

  // The two search groups resolve independently, so a title match can render
  // above an already-highlighted content match. Keep the top row selected
  // until the user moves the selection for this query.
  const firstItemValue =
    visibleSessions[0]?.id ??
    contentSessions[0]?.id ??
    (filteredActions[0] ? `action:${filteredActions[0].id}` : "");
  const [selectedValue, setSelectedValue] = useState("");
  const userMovedSelection = useRef(false);
  useEffect(() => {
    userMovedSelection.current = false;
  }, [open, debouncedQuery]);
  useEffect(() => {
    if (!userMovedSelection.current) setSelectedValue(firstItemValue);
  }, [firstItemValue, open, debouncedQuery]);

  const paletteLabel = sessionsOnly ? "Switch session" : "Command palette";
  const placeholder = sessionsOnly
    ? "Search sessions by name…"
    : "Search sessions or run a command";

  const runAction = (action: ActionCommand): void => {
    close();
    action.run();
  };

  const goToSession = (id: string): void => {
    rememberSearchRow(id);
    close();
    navigate(`/c/${id}`);
  };

  const renderSession = (s: PaletteSession): React.ReactNode => (
    // pl-6 indents the label to line up with the icon-prefixed Action rows
    // below (their 16px icon + 8px gap), so the groups read as one column.
    <CommandItem
      key={s.id}
      value={s.id}
      onSelect={() => goToSession(s.id)}
      className="items-start pl-6"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate text-left">
          <HighlightedText text={s.label} query={sessionsOnly ? query : debouncedQuery} />
        </span>
        {s.snippet && (
          // Where the match was found in the chat body — the session is often
          // unidentifiable from the title alone.
          <span className="truncate text-left text-muted-foreground text-sm">
            <HighlightedText text={s.snippet} query={debouncedQuery} />
          </span>
        )}
      </div>
      {s.archived && (
        <Badge variant="outline" className="ml-2 text-muted-foreground">
          Archived
        </Badge>
      )}
      <span className="ml-2 shrink-0 text-sm text-muted-foreground">{s.agent}</span>
    </CommandItem>
  );

  const renderShowMore = (
    scope: "title" | "content",
    group: typeof titleSearch,
    rows: readonly PaletteSession[],
  ): React.ReactNode =>
    group.hasNextPage &&
    !group.isFetchNextPageError && (
      <CommandItem
        value={`more:${scope}`}
        disabled={group.isFetchingNextPage}
        onSelect={() => {
          // This row vanishes after the last page, so park the selection on
          // the group's last row; the next ArrowDown reaches the new rows.
          userMovedSelection.current = true;
          const lastRow = rows.at(-1);
          if (lastRow) setSelectedValue(lastRow.id);
          void group.fetchNextPage();
        }}
        className="pl-6 text-muted-foreground"
      >
        {group.isFetchingNextPage ? "Loading…" : "Show more"}
      </CommandItem>
    );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        // Mobile: a top-anchored full-screen sheet sized to --omnigent-viewport-height,
        // so the input and results sit above the soft keyboard. sm:max-w-full
        // overrides DialogContent's sm:max-w-sm between 640px and md.
        className={cn(
          "overflow-hidden p-0",
          isMobile
            ? "inset-x-0 top-0 h-full max-h-full w-full max-w-full translate-x-0 translate-y-0 gap-0 rounded-none border-0 shadow-none sm:max-w-full"
            : "top-1/4 translate-y-0 rounded-xl sm:max-w-2xl",
        )}
        style={
          isMobile
            ? {
                top: 0,
                height: "var(--omnigent-viewport-height, 100dvh)",
                maxHeight: "var(--omnigent-viewport-height, 100dvh)",
                // Pad both insets: the macOS title bar or safe-top clears the window
                // controls or notch; safe-bottom keeps the last row above the home
                // indicator when the keyboard is closed (the viewport then spans it).
                paddingTop: "var(--mac-titlebar-height, var(--omnigent-safe-top, 0px))",
                paddingBottom: "var(--omnigent-safe-bottom, 0px)",
              }
            : undefined
        }
        showCloseButton={false}
      >
        <DialogTitle className="sr-only">{paletteLabel}</DialogTitle>
        {/* shouldFilter=false: the server filters sessions and we filter actions
            (see file header). vimBindings=false: keep Ctrl+K/J from doubling as
            list-nav on Win/Linux, where Ctrl+K is also the opener. */}
        {/* Command's base class is `size-full`, so it already fills the sheet. */}
        <Command
          shouldFilter={false}
          vimBindings={false}
          label={paletteLabel}
          value={selectedValue}
          onValueChange={setSelectedValue}
          onKeyDown={(event) => {
            if (SELECTION_KEYS.has(event.key)) userMovedSelection.current = true;
          }}
        >
          {isMobile ? (
            // Search field and an explicit close button share a top row; the
            // full-screen sheet has no ⌘K/Esc affordance the way the desktop
            // dialog does, so the X is how touch users dismiss it.
            <div className="flex items-center gap-1 p-1">
              <div className="min-w-0 flex-1">
                <CommandInput
                  value={query}
                  onValueChange={setQuery}
                  placeholder={placeholder}
                  data-testid="command-palette-input"
                />
              </div>
              <Button
                variant="ghost"
                size="icon-lg"
                className="shrink-0 rounded-full"
                onClick={close}
                aria-label="Close search"
              >
                <XIcon className="size-5 text-muted-foreground" />
              </Button>
            </div>
          ) : (
            <CommandInput
              value={query}
              onValueChange={setQuery}
              placeholder={placeholder}
              data-testid="command-palette-input"
            />
          )}
          <CommandList
            className={isMobile ? "max-h-none flex-1" : undefined}
            // Hovering a row selects it in cmdk, which counts as the user's pick.
            onPointerMove={(event) => {
              if ((event.target as Element).closest("[cmdk-item]")) {
                userMovedSelection.current = true;
              }
            }}
          >
            {(loadError || (sessionsOnly && isFetching)) && (
              <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
                {loadError
                  ? loadMoreError
                    ? "Couldn't load more sessions."
                    : "Couldn't load sessions."
                  : "Loading sessions…"}
                {loadError && (
                  <button
                    type="button"
                    className="ml-2 underline"
                    disabled={retrying}
                    onClick={retryLoad}
                  >
                    Retry
                  </button>
                )}
              </p>
            )}
            {!loadError && !(sessionsOnly && isFetching) && (
              <CommandEmpty>{searchFetching ? "Searching…" : "No results found"}</CommandEmpty>
            )}
            {sessions.length > 0 && (
              <CommandGroup heading="Sessions">
                {visibleSessions.map(renderSession)}
                {searching && renderShowMore("title", titleSearch, visibleSessions)}
              </CommandGroup>
            )}
            {contentSessions.length > 0 && (
              <CommandGroup heading="In messages">
                {contentSessions.map(renderSession)}
                {renderShowMore("content", contentSearch, contentSessions)}
              </CommandGroup>
            )}
            {sessionsOnly && sessions.length > visibleSessions.length && (
              <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
                Showing {visibleSessions.length} of {sessions.length} matches. Type more to narrow
                your search.
              </p>
            )}
            {sessionsOnly && olderSessionsAvailable && !loadError && (
              <Button
                variant="ghost"
                className="w-full"
                disabled={isFetching}
                onClick={searchOlderSessions}
              >
                Search older sessions
              </Button>
            )}
            {filteredActions.length > 0 && (
              <CommandGroup heading="Actions">
                {filteredActions.map((a) => {
                  const Icon = a.icon;
                  return (
                    <CommandItem key={a.id} value={`action:${a.id}`} onSelect={() => runAction(a)}>
                      <Icon />
                      <span className="flex-1 truncate text-left">{a.label}</span>
                      {a.shortcut && <CompactShortcutKeys keys={a.shortcut} />}
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
