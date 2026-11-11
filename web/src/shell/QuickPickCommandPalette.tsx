// ⌘K on QuickPick: the same Sessions, In messages and Actions rows as CommandPalette,
// with fuzzy-ranked actions and keyboard navigation from the QuickPick list.

import { useEffect, useState } from "react";
import { XIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { QuickPick } from "@/components/ui/quick-pick/QuickPick";
import { occurrenceRanges } from "@/components/ui/quick-pick/match";
import type { QuickPickItem } from "@/components/ui/quick-pick/model";
import type { QuickPickSegment } from "@/components/ui/quick-pick/presentation";
import { type ActionCommand, useAppCommands } from "./appCommands";
import type { CommandPaletteProps } from "./CommandPalette";
import { type PaletteSession, usePaletteSessionSearch } from "./usePaletteSessionSearch";

type PaletteRow =
  | { kind: "session"; id: string }
  | { kind: "action"; action: ActionCommand }
  | { kind: "more"; load: () => void; lastRowId: string | undefined };

/** A blank icon slot, so session labels line up with the icon-prefixed actions. */
const SESSION_ICON = <span aria-hidden className="size-4" />;

export function QuickPickCommandPalette({
  sessionsOnly = false,
  open,
  onOpenChange,
  onToggleLeftSidebar,
  onToggleRightSidebar,
  rightSidebarShortcutEnabled = true,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const search = usePaletteSessionSearch({ open, sessionsOnly });
  const { query, debouncedQuery, titleSearch, contentSearch } = search;
  const close = (): void => onOpenChange(false);
  const actions = useAppCommands({
    onToggleLeftSidebar,
    onToggleRightSidebar,
    rightSidebarShortcutEnabled,
  });
  // Show more parks the selection on its group's last row, so the next ArrowDown
  // reaches the rows it loads; the user's next move clears it.
  const [parkedId, setParkedId] = useState<string | null>(null);
  // Every opening starts from the top row, as the query does.
  useEffect(() => {
    if (!open) setParkedId(null);
  }, [open]);

  const highlightQuery = sessionsOnly ? query : debouncedQuery;
  const sessionItem = (session: PaletteSession, group: string): QuickPickItem<PaletteRow> => ({
    id: session.id,
    label: session.label,
    description: session.snippet ?? undefined,
    group,
    icon: SESSION_ICON,
    highlights: {
      label: occurrenceRanges(highlightQuery, session.label),
      description: session.snippet ? occurrenceRanges(debouncedQuery, session.snippet) : [],
    },
    accessory: (
      <>
        {session.archived && (
          <Badge variant="outline" className="text-muted-foreground">
            Archived
          </Badge>
        )}
        <span className="text-sm text-muted-foreground">{session.agent}</span>
      </>
    ),
    value: { kind: "session", id: session.id },
  });
  const showMoreItem = (
    scope: "title" | "content",
    group: typeof titleSearch,
    rows: readonly PaletteSession[],
    groupLabel: string,
  ): QuickPickItem<PaletteRow>[] =>
    // A group's paging row shows only under rows of that group, as its heading does.
    rows.length > 0 && group.hasNextPage && !group.isFetchNextPageError
      ? [
          {
            id: `more:${scope}`,
            label: group.isFetchingNextPage ? "Loading…" : "Show more",
            group: groupLabel,
            icon: SESSION_ICON,
            disabledReason: group.isFetchingNextPage ? "Loading more sessions" : undefined,
            value: {
              kind: "more",
              load: () => void group.fetchNextPage(),
              lastRowId: rows.at(-1)?.id,
            },
          },
        ]
      : [];

  const { visibleSessions, contentSessions, searching } = search;
  const segments: QuickPickSegment<PaletteRow>[] = [
    {
      sourceId: "sessions",
      ranking: "provider",
      items: [
        ...visibleSessions.map((session) => sessionItem(session, "Sessions")),
        ...(searching ? showMoreItem("title", titleSearch, visibleSessions, "Sessions") : []),
      ],
    },
    {
      sourceId: "messages",
      ranking: "provider",
      items: [
        ...contentSessions.map((session) => sessionItem(session, "In messages")),
        ...showMoreItem("content", contentSearch, contentSessions, "In messages"),
      ],
    },
    {
      sourceId: "actions",
      ranking: "client",
      fuzzyMatching: "words",
      items: sessionsOnly
        ? []
        : actions.map((action) => ({
            id: `action:${action.id}`,
            label: action.label,
            keywords: action.keywords,
            group: "Actions",
            icon: <action.icon />,
            shortcut: action.shortcut,
            value: { kind: "action", action },
          })),
    },
  ];

  const accept = (item: QuickPickItem<PaletteRow>): void => {
    const row = item.value;
    if (row.kind === "more") {
      setParkedId(row.lastRowId ?? null);
      row.load();
      return;
    }
    close();
    if (row.kind === "action") {
      row.action.run();
      return;
    }
    search.rememberSearchRow(row.id);
    navigate(`/c/${row.id}`);
  };

  const label = sessionsOnly ? "Switch session" : "Command palette";
  const loadingSessions = sessionsOnly && search.isFetching;
  const loadStatus =
    search.loadError || loadingSessions ? (
      <p className="px-3 py-2 text-xs text-muted-foreground">
        {search.loadError
          ? search.loadMoreError
            ? "Couldn't load more sessions."
            : "Couldn't load sessions."
          : "Loading sessions…"}
        {search.loadError && (
          <button
            type="button"
            className="ml-2 underline"
            disabled={search.retrying}
            onClick={search.retryLoad}
          >
            Retry
          </button>
        )}
      </p>
    ) : null;
  // The cap stays visible with a load message, so hidden matches are never a surprise.
  const capStatus = sessionsOnly && search.sessions.length > search.visibleSessions.length && (
    <p className="px-3 py-2 text-xs text-muted-foreground">
      Showing {search.visibleSessions.length} of {search.sessions.length} matches. Type more to
      narrow your search.
    </p>
  );
  const status =
    loadStatus || capStatus ? (
      <>
        {loadStatus}
        {capStatus}
      </>
    ) : undefined;
  const olderSessions = sessionsOnly && search.olderSessionsAvailable && !search.loadError && (
    <Button
      variant="ghost"
      className="w-full"
      disabled={search.isFetching}
      onClick={search.searchOlderSessions}
    >
      Search older sessions
    </Button>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
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
                paddingTop: "var(--mac-titlebar-height, var(--omnigent-safe-top, 0px))",
                paddingBottom: "var(--omnigent-safe-bottom, 0px)",
              }
            : undefined
        }
        showCloseButton={false}
      >
        <DialogTitle className="sr-only">{label}</DialogTitle>
        <QuickPick
          label={label}
          placeholder={
            sessionsOnly ? "Search sessions by name…" : "Search sessions or run a command"
          }
          segments={segments}
          query={query}
          onQueryChange={(next) => {
            setParkedId(null);
            search.setQuery(next);
          }}
          onAccept={accept}
          activeId={parkedId}
          onActiveIdChange={() => setParkedId(null)}
          inputTestId="command-palette-input"
          emptyMessage={search.searchFetching ? "Searching…" : "No results found"}
          status={status}
          listEnd={olderSessions}
          inputEnd={
            isMobile && (
              // The full-screen sheet has no ⌘K/Esc affordance, so touch users close it here.
              <Button
                variant="ghost"
                size="icon-lg"
                className="shrink-0 rounded-full"
                onClick={close}
                aria-label="Close search"
              >
                <XIcon className="size-5 text-muted-foreground" />
              </Button>
            )
          }
          fillHeight={isMobile}
        />
      </DialogContent>
    </Dialog>
  );
}
