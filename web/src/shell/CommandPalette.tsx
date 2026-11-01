// ⌘K palette: Sessions (recent, or title matches), In messages (content matches),
// Actions, then New chat in project (one row per project, once the user types).
// Sessions are searched server-side (one loaded page can't find older ones), so
// cmdk filtering is off and the static actions and projects are filtered here.
// With `projectStep` it instead asks which project a new session belongs to:
// "No project" first, then every project, filtered as you type.

import type React from "react";
import { defaultFilter } from "cmdk";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarClockIcon,
  CircleSlashIcon,
  InboxIcon,
  type LucideIcon,
  PanelLeftIcon,
  PanelRightIcon,
  SettingsIcon,
  SquarePenIcon,
  XIcon,
} from "lucide-react";
import { useNavigate } from "@/lib/routing";
import {
  type ConversationsPage,
  useConversations,
  useProjects,
  useSessionSearch,
} from "@/hooks/useConversations";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { BOOT_PROBE_TIMEOUT_MS } from "@/lib/bootCapabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
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
import { ProjectRowIcon } from "./ProjectPicker";
import { conversationDisplayLabel, getConversationAgentType } from "./sidebarNav";

export interface CommandPaletteProps {
  sessionsOnly?: boolean;
  /** Ask which project a new session belongs to instead of listing commands. */
  projectStep?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Flip the left (Conversations) sidebar — owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar — owned by AppShell. */
  onToggleRightSidebar: () => void;
  /** Start a new session, which may reopen the palette on the project step.
      Defaults to opening the plain composer. */
  onNewSession?: () => void;
}

interface ActionCommand {
  id: string;
  label: string;
  /** Mirrors the icon on the equivalent button elsewhere in the UI. */
  icon: LucideIcon;
  /** Extra terms the client-side filter matches against (beyond the label). */
  keywords: string[];
  run: () => void;
}

/** Debounce matches the sidebar search (300ms) so keystrokes don't each fetch. */
const SEARCH_DEBOUNCE_MS = 300;

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

/** How many recent sessions to show before the user types, so the Actions
    group stays visible without scrolling. Typing lifts the cap. */
const IDLE_SESSION_LIMIT = 5;
const SESSION_SEARCH_PAGE_BATCH = 10;
const SESSION_SEARCH_RESULT_LIMIT = 50;
/** Rows per page of each search group; "Show more" loads the next page. */
const SEARCH_GROUP_PAGE_SIZE = 8;
/** Keys that move cmdk's selection; see `userMovedSelection`. */
const SELECTION_KEYS = new Set(["ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

interface PaletteSession {
  id: string;
  label: string;
  agent: string;
  archived: boolean;
  snippet: string | null;
}

/** Flatten list pages into palette rows, dropping repeated ids and, unless
    `includeArchived`, archived sessions. */
function paletteSessions(
  pages: readonly ConversationsPage[],
  {
    includeArchived = false,
    excludeIds = new Set(),
  }: { includeArchived?: boolean; excludeIds?: ReadonlySet<string> } = {},
): PaletteSession[] {
  const seen = new Set(excludeIds);
  const out: PaletteSession[] = [];
  for (const page of pages) {
    for (const c of page.data) {
      if ((c.archived && !includeArchived) || seen.has(c.id)) continue;
      seen.add(c.id);
      out.push({
        id: c.id,
        label: conversationDisplayLabel(c),
        agent: getConversationAgentType(c),
        archived: c.archived === true,
        // Present only when the match was in chat content (not the title);
        // the server omits it otherwise. Shown as a dimmed second line.
        snippet: c.search_snippet ?? null,
      });
    }
  }
  return out;
}

/** Whether every whitespace-separated word of `query` occurs in `text`. */
function matchesEveryWord(text: string, query: string): boolean {
  const lower = text.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => lower.includes(word));
}

export function CommandPalette({
  sessionsOnly = false,
  projectStep = false,
  open,
  onOpenChange,
  onToggleLeftSidebar,
  onToggleRightSidebar,
  onNewSession,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [pageLimit, setPageLimit] = useState(SESSION_SEARCH_PAGE_BATCH);

  // Reset the query when the palette closes so it reopens clean.
  useEffect(() => {
    if (!open) {
      setQuery("");
      setDebouncedQuery("");
      setPageLimit(SESSION_SEARCH_PAGE_BATCH);
    }
  }, [open]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const close = (): void => onOpenChange(false);

  const actions = useMemo<ActionCommand[]>(
    () => [
      {
        id: "new-chat",
        label: "New chat",
        icon: SquarePenIcon,
        keywords: ["compose", "start", "new session"],
        run: onNewSession ?? (() => navigate("/")),
      },
      {
        id: "go-inbox",
        label: "Go to Inbox",
        icon: InboxIcon,
        keywords: ["notifications", "comments", "needs response"],
        run: () => navigate("/inbox"),
      },
      {
        id: "go-tasks",
        label: "Go to Automations",
        icon: CalendarClockIcon,
        keywords: ["scheduled", "recurring", "cron", "automation", "schedule"],
        run: () => navigate("/tasks"),
      },
      {
        id: "go-settings",
        label: "Go to Settings",
        icon: SettingsIcon,
        keywords: ["preferences", "configuration", "account"],
        run: () => navigate("/settings"),
      },
      {
        id: "toggle-left-sidebar",
        label: "Toggle conversations sidebar",
        icon: PanelLeftIcon,
        keywords: ["panel", "left", "sessions list"],
        run: onToggleLeftSidebar,
      },
      {
        id: "toggle-right-sidebar",
        label: "Toggle workspace sidebar",
        icon: PanelRightIcon,
        keywords: ["panel", "right", "files", "terminal"],
        run: onToggleRightSidebar,
      },
    ],
    [navigate, onNewSession, onToggleLeftSidebar, onToggleRightSidebar],
  );

  const filteredActions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (sessionsOnly || projectStep) return [];
    if (q === "") return actions;
    return actions.filter(
      (a) =>
        a.label.toLowerCase().includes(q) || a.keywords.some((k) => k.toLowerCase().includes(q)),
    );
  }, [actions, query, sessionsOnly, projectStep]);

  const { data: projects = [], isSuccess: projectsLoaded, isError: projectsFailed } = useProjects();
  // Every typed word must appear in the row label or the group heading, so
  // "new omni", "omni" and "project" all reach "New chat in omnigent". The
  // project step lists every project until the user types, and matches names only.
  const filteredProjects = useMemo(() => {
    if (projectStep) return projects.filter((project) => matchesEveryWord(project.name, query));
    if (sessionsOnly || query.trim() === "") return [];
    return projects.filter((project) =>
      matchesEveryWord(`new chat in project ${project.name}`, query),
    );
  }, [projects, query, sessionsOnly, projectStep]);
  const showNoProject = projectStep && matchesEveryWord("no project", query);
  // The project step may open before the list resolves. Nothing to choose
  // from means the plain composer, as if the prompt had never been offered.
  const noProjectToOffer = projectsFailed || (projectsLoaded && projects.length === 0);
  useEffect(() => {
    if (!open || !projectStep || !noProjectToOffer) return;
    onOpenChange(false);
    navigate("/");
  }, [open, projectStep, noProjectToOffer, onOpenChange, navigate]);

  const { data, isFetching, isError, refetch, hasNextPage, fetchNextPage, isFetchNextPageError } =
    useConversations("", false, { enabled: open && !projectStep });
  const serverInfo = useServerInfo();
  // Older servers ignore `search_scope`, so they get one mixed group instead.
  const groupedSearch = serverInfo !== "loading" && serverInfo.session_search_scope === true;
  // The project step filters projects locally and never searches sessions.
  const searching = !sessionsOnly && !projectStep && debouncedQuery !== "";
  // Wait briefly for server info so a supporting server never gets the mixed
  // request; if it stalls, fall back to that request rather than never searching.
  const serverInfoLoading = serverInfo === "loading";
  const [serverInfoWaitExpired, setServerInfoWaitExpired] = useState(false);
  useEffect(() => {
    if (!serverInfoLoading || !searching || serverInfoWaitExpired) return;
    const timer = setTimeout(() => setServerInfoWaitExpired(true), BOOT_PROBE_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [serverInfoLoading, searching, serverInfoWaitExpired]);
  const awaitingServerInfo = serverInfoLoading && !serverInfoWaitExpired;
  const searchOptions = {
    enabled: open && searching && !awaitingServerInfo,
    limit: SEARCH_GROUP_PAGE_SIZE,
  };
  const titleSearch = useSessionSearch(
    debouncedQuery,
    groupedSearch ? "title" : "all",
    searchOptions,
  );
  const contentSearch = useSessionSearch(debouncedQuery, "content", {
    ...searchOptions,
    enabled: searchOptions.enabled && groupedSearch,
  });
  const loadedPages = data?.pages.length ?? 0;
  useEffect(() => {
    if (
      open &&
      sessionsOnly &&
      hasNextPage &&
      loadedPages < pageLimit &&
      !isFetching &&
      !isError &&
      !isFetchNextPageError
    ) {
      void fetchNextPage();
    }
  }, [
    open,
    sessionsOnly,
    hasNextPage,
    loadedPages,
    pageLimit,
    isFetching,
    isError,
    isFetchNextPageError,
    fetchNextPage,
  ]);

  const sessions = useMemo(() => {
    // A disabled query still returns cached pages; the project step lists none.
    if (projectStep) return [];
    if (searching) return paletteSessions(titleSearch.data?.pages ?? [], { includeArchived: true });
    // Rows archived after they loaded are still cached here, so drop them.
    const out = paletteSessions(data?.pages ?? []);
    if (sessionsOnly) {
      if (!query.trim()) return out;
      return out
        .map((session) => ({ ...session, score: defaultFilter(session.label, query.trim()) }))
        .filter((session) => session.score > 0)
        .sort((first, second) => second.score - first.score);
    }
    // The full first page would push Actions below the fold.
    return out.slice(0, IDLE_SESSION_LIMIT);
  }, [data, titleSearch.data, query, searching, sessionsOnly, projectStep]);
  // Excludes title matches again in case a rename moved a session between the
  // two groups' requests: cmdk needs each item value to be unique.
  const contentSessions = useMemo(
    () =>
      searching
        ? paletteSessions(contentSearch.data?.pages ?? [], {
            includeArchived: true,
            excludeIds: new Set(sessions.map((s) => s.id)),
          })
        : [],
    [contentSearch.data, searching, sessions],
  );
  const visibleSessions = sessionsOnly ? sessions.slice(0, SESSION_SEARCH_RESULT_LIMIT) : sessions;
  const searchGroups = [titleSearch, contentSearch];
  // The project step never reads sessions, so a stale fetch error stays hidden.
  const loadError = searching
    ? searchGroups.some((group) => group.isError)
    : !projectStep && (isError || isFetchNextPageError);
  const loadMoreError = searching
    ? searchGroups.some((group) => group.isFetchNextPageError)
    : isFetchNextPageError;
  const searchFetching =
    (searching && awaitingServerInfo) || searchGroups.some((group) => group.isFetching);
  const retrying = searching
    ? searchGroups.some((group) => group.isError && group.isFetching)
    : isFetching;
  const retryLoad = (): void => {
    if (!searching) {
      void (isFetchNextPageError ? fetchNextPage() : refetch());
      return;
    }
    for (const group of searchGroups) {
      if (group.isFetchNextPageError) void group.fetchNextPage();
      else if (group.isError) void group.refetch();
    }
  };

  // The two search groups resolve independently, so a title match can render
  // above an already-highlighted content match. Keep the top row selected
  // until the user moves the selection for this query.
  const firstItemValue =
    visibleSessions[0]?.id ??
    contentSessions[0]?.id ??
    (filteredActions[0] ? `action:${filteredActions[0].id}` : null) ??
    (showNoProject ? "no-project" : null) ??
    (filteredProjects[0] ? `project:${filteredProjects[0].name}` : "");
  const [selectedValue, setSelectedValue] = useState("");
  const userMovedSelection = useRef(false);
  useEffect(() => {
    userMovedSelection.current = false;
  }, [open, debouncedQuery]);
  useEffect(() => {
    if (!userMovedSelection.current) setSelectedValue(firstItemValue);
  }, [firstItemValue, open, debouncedQuery]);

  const paletteLabel = projectStep
    ? "New session"
    : sessionsOnly
      ? "Switch session"
      : "Command palette";
  const placeholder = projectStep
    ? "Start a new session in…"
    : sessionsOnly
      ? "Search sessions by name…"
      : "Search sessions or run a command";

  const runAction = (action: ActionCommand): void => {
    close();
    action.run();
  };

  const newChatInProject = (name: string): void => {
    close();
    navigate(name ? `/?project=${encodeURIComponent(name)}` : "/");
  };

  const goToSession = (id: string): void => {
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
            {sessionsOnly && hasNextPage && loadedPages >= pageLimit && !loadError && (
              <Button
                variant="ghost"
                className="w-full"
                disabled={isFetching}
                onClick={() => setPageLimit(loadedPages + SESSION_SEARCH_PAGE_BATCH)}
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
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            )}
            {(filteredProjects.length > 0 || showNoProject) && (
              <CommandGroup heading={projectStep ? "Project" : "New chat in project"}>
                {showNoProject && (
                  <CommandItem value="no-project" onSelect={() => newChatInProject("")}>
                    <CircleSlashIcon />
                    <span className="flex-1 truncate text-left">No project</span>
                  </CommandItem>
                )}
                {filteredProjects.map((project) => (
                  <CommandItem
                    key={project.name}
                    value={`project:${project.name}`}
                    onSelect={() => newChatInProject(project.name)}
                  >
                    <ProjectRowIcon icon={project.icon} className="size-4 text-[16px]" />
                    <span className="flex-1 truncate text-left">
                      {projectStep ? project.name : `New chat in ${project.name}`}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
