// ⌘K on QuickPick: the same Sessions, In messages and Actions rows as CommandPalette,
// with fuzzy-ranked actions and keyboard navigation from the QuickPick list, and the
// commands features on screen offer through `usePaletteCommands`, under their headings.

import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import { XIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  type QuickPickAcceptResult,
  QuickPickDialog,
  type QuickPickHandOff,
  type QuickPickItem,
  type QuickPickSegment,
} from "@/components/ui/quick-pick";
import { occurrenceRanges } from "@/components/ui/quick-pick/match";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { useAppCommands } from "./appCommands";
import { SESSION_SWITCHER, useQuickPickAppCommands } from "./quickPickAppCommands";
import type { CommandPaletteProps } from "./CommandPalette";
import {
  type CommandRegion,
  commandTargetKey,
  useCommandContext,
  useCommandRegion,
  useCommandRegionReader,
} from "./commandContext";
import { useCommandOptionsPage } from "./commandOptionsPage";
import {
  type PaletteParentPlace,
  paletteChildPageStore,
  resetPaletteToRoot,
} from "./commandPaletteChildPage";
import { currentPaletteCommand, paletteCommandItems } from "./paletteCommandRows";
import { PaletteSessionContext } from "./PaletteSessionContext";
import type { PaletteTarget as PaletteRow } from "./paletteTarget";
import { useSettingChoicesPage } from "./settingChoicesPage";
import { type PaletteSession, usePaletteSessionSearch } from "./usePaletteSessionSearch";

const COMMAND_HISTORY_KEY = "command-palette:commands";

/** The child pages' copy; their headings name what they act on. */
const CHILD_PAGE_COPY = {
  "setting-choices": {
    label: "Choose a value",
    placeholder: "Choose a value…",
    empty: "No matching values.",
  },
  "command-options": {
    label: "Choose one",
    placeholder: "Choose one…",
    empty: "Nothing to choose from.",
  },
} as const;

/** The footer's line for what a page waits on, or why a choice didn't apply. */
function pageStatusSummary(text: string): () => ReactNode {
  return function PageStatus() {
    return (
      <span role="status" data-slot="palette-page-status">
        {text}
      </span>
    );
  };
}

/** The phone sheet fills the keyboard-aware viewport, clear of the title bar and home bar. */
const MOBILE_SHEET_STYLE: CSSProperties = {
  top: 0,
  height: "var(--omnigent-viewport-height, 100dvh)",
  maxHeight: "var(--omnigent-viewport-height, 100dvh)",
  paddingTop: "var(--mac-titlebar-height, var(--omnigent-safe-top, 0px))",
  paddingBottom: "var(--omnigent-safe-bottom, 0px)",
};

/** A blank icon slot, so session labels line up with the icon-prefixed actions. */
const SESSION_ICON = <span aria-hidden className="size-4" />;

export function QuickPickCommandPalette({
  sessionsOnly = false,
  open,
  onOpenChange,
  onToggleLeftSidebar,
  onToggleRightSidebar,
  rightSidebarShortcutEnabled = true,
  onGoToSession,
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
  const featureGroups = usePaletteCommandsStore((state) => state.groups);
  const context = useCommandContext();
  // Only an open palette follows focus between regions; a closed one skips those renders.
  const region = useCommandRegion(open);
  const readRegion = useCommandRegionReader();
  const targetKey = commandTargetKey(context, region);
  const appCommands = useQuickPickAppCommands({ onGoToSession });
  // Every registry row: the app's own, ungrouped, then each feature's under its heading.
  const commandItems = useMemo(
    () => paletteCommandItems(appCommands, featureGroups, targetKey),
    [appCommands, featureGroups, targetKey],
  );
  const latestContext = useRef(context);
  latestContext.current = context;
  const latestAppCommands = useRef(appCommands);
  latestAppCommands.current = appCommands;
  /** The command row `itemId` offers now for `rowTargetKey`, with focus in `inRegion`. */
  const commandFor = (
    itemId: string,
    rowTargetKey: string,
    inRegion: CommandRegion | null = readRegion(),
  ) =>
    currentPaletteCommand(itemId, rowTargetKey, {
      appCommands: latestAppCommands.current,
      featureGroups: usePaletteCommandsStore.getState().groups,
      targetKey: commandTargetKey(latestContext.current, inRegion),
    });
  /** The command `item` runs now, or null once it no longer applies. */
  const currentCommand = (item: QuickPickItem<PaletteRow>, inRegion?: CommandRegion | null) =>
    item.value.kind === "command" ? commandFor(item.id, item.value.targetKey, inRegion) : null;
  // Show more parks the selection on its group's last row, so the next ArrowDown
  // reaches the rows it loads, and Back makes the row that opened a page active again;
  // the user's next move clears it.
  const [parkedId, setParkedId] = useState<string | null>(null);
  // A child page has its own query; the root's stays as it was for Back.
  const [pageQuery, setPageQuery] = useState("");
  // Every opening starts from the top row of a fresh root, and a close drops what a page awaited.
  useEffect(() => {
    if (!open) setParkedId(null);
    resetPaletteToRoot();
  }, [open]);
  const childPage = useStore(paletteChildPageStore, (state) => state.page);
  // Back on the row that opened the page, once a choice applies or its setting goes away.
  const restoreParent = (parent: PaletteParentPlace | null) => {
    setPageQuery("");
    if (parent && parent.query !== query) search.setQuery(parent.query);
    setParkedId(parent?.activeId ?? null);
  };
  const settingPage = useSettingChoicesPage(commandItems, targetKey, restoreParent);
  const optionsPage = useCommandOptionsPage(
    (itemId, pageTargetKey) => commandFor(itemId, pageTargetKey),
    restoreParent,
  );

  const highlightQuery = sessionsOnly ? query : debouncedQuery;
  const sessionItem = (session: PaletteSession, group: string): QuickPickItem<PaletteRow> => ({
    id: session.id,
    label: session.label,
    context: <PaletteSessionContext project={session.project} parent={session.parent} />,
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
    // Sessions list by their own activity; the history holds commands only.
    rememberInHistory: false,
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
            rememberInHistory: false,
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
  const activePage =
    childPage?.kind === "setting-choices" ? settingPage : childPage ? optionsPage : null;
  const pageItems = activePage?.items ?? null;
  const rootSegments: QuickPickSegment<PaletteRow>[] = [
    {
      sourceId: "sessions",
      ranking: "provider",
      origin: "source",
      items: [
        ...visibleSessions.map((session) => sessionItem(session, "Sessions")),
        ...(searching ? showMoreItem("title", titleSearch, visibleSessions, "Sessions") : []),
      ],
    },
    {
      sourceId: "messages",
      ranking: "provider",
      origin: "source",
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
        : [
            ...actions.map((action): QuickPickItem<PaletteRow> => ({
              id: `action:${action.id}`,
              label: action.label,
              keywords: action.keywords,
              group: "Actions",
              // A command is a short name; a quiet underline marks the words the query hit.
              matchHighlight: "underline",
              icon: <action.icon />,
              shortcut: action.shortcut,
              value: { kind: "action", action },
            })),
            // Registry commands without a feature are the app's own, so they list as Actions.
            ...commandItems
              .filter((item) => item.group === undefined)
              .map((item): QuickPickItem<PaletteRow> => ({
                ...item,
                group: "Actions",
                matchHighlight: "underline",
              })),
          ],
    },
    {
      sourceId: "features",
      ranking: "client",
      fuzzyMatching: "words",
      items: sessionsOnly
        ? []
        : commandItems
            .filter((item) => item.group !== undefined)
            .map((item) => ({ ...item, matchHighlight: "underline" })),
    },
  ];
  const segments: QuickPickSegment<PaletteRow>[] = pageItems
    ? [{ sourceId: "page", ranking: "client", fuzzyMatching: "words", items: pageItems }]
    : rootSegments;

  const accept = (item: QuickPickItem<PaletteRow>): QuickPickAcceptResult | QuickPickHandOff => {
    const row = item.value;
    if (row.kind === "more") {
      setParkedId(row.lastRowId ?? null);
      row.load();
      return "keepOpen";
    }
    if (row.kind === "action") {
      row.action.run();
      return undefined;
    }
    if (row.kind === "setting-choice") return settingPage.acceptChoice(row.value);
    if (row.kind === "command-option") return optionsPage.acceptOption(row.optionId);
    if (row.kind === "command") {
      const command = currentCommand(item);
      // A row withdrawn or built for another target since stays open, now showing what applies.
      if (!command) return "keepOpen";
      // A setting's row opens its values, with the one the query named active.
      if (command.setting) {
        const activeChoice = settingPage.openChoices(command, item.id, row.targetKey, {
          query,
          activeId: item.id,
        });
        setPageQuery("");
        setParkedId(activeChoice);
        return "keepOpen";
      }
      if (command.options) {
        optionsPage.openOptions(command, item.id, row.targetKey, { query, activeId: item.id });
        setPageQuery("");
        setParkedId(null);
        return "keepOpen";
      }
      if (command.keepsPaletteOpen) {
        command.run();
        return command.continuesAs ? { handOffTo: command.continuesAs } : "keepOpen";
      }
      // A command that moves focus runs once the palette has closed and restored it; it's read
      // again then, in case the close changed what applies.
      if (command.movesFocus) {
        const acceptedIn = readRegion();
        return { afterClose: () => currentCommand(item, acceptedIn)?.run() };
      }
      command.run();
      return undefined;
    }
    search.rememberSearchRow(row.id);
    navigate(`/c/${row.id}`);
    return undefined;
  };

  const label = sessionsOnly ? "Switch session" : "Command palette";
  const loadingSessions = sessionsOnly && search.isFetching;
  const loadStatus =
    search.loadError || loadingSessions ? (
      <p className="px-3 py-2 text-xs text-muted-foreground">
        <span role="status">
          {search.loadError
            ? search.loadMoreError
              ? "Couldn't load more sessions."
              : "Couldn't load sessions."
            : "Loading sessions…"}
        </span>
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
  // As in today's palette, a load error or loading message stands in for the empty one.
  const rootEmptyMessage = (): string | null => {
    if (loadStatus) return null;
    return search.searchFetching ? "Searching…" : "No results found";
  };
  // The cap stays visible with a load message, so hidden matches are never a surprise.
  const capStatus = sessionsOnly && search.sessions.length > search.visibleSessions.length && (
    <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
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

  const pageCopy = childPage ? CHILD_PAGE_COPY[childPage.kind] : null;
  // What a page waits on or why a choice didn't apply; on the root, that a page's setting went away.
  const pageStatus = settingPage.open
    ? settingPage.status
    : optionsPage.open
      ? optionsPage.status
      : settingPage.notice;
  const back = () => restoreParent(settingPage.open ? settingPage.back() : optionsPage.back());
  const pageHeading = settingPage.heading ?? optionsPage.heading;

  return (
    <QuickPickDialog
      open={open}
      onOpenChange={onOpenChange}
      // Go to Session… hands the palette's return target to the switcher opening in its place.
      successorName={sessionsOnly ? SESSION_SWITCHER : undefined}
      // ⌘K steps with Ctrl-N/Ctrl-P, trading the query's readline caret keys for row steps.
      stepsWithCtrlNP
      contentClassName={
        isMobile
          ? "inset-x-0 flex w-full max-w-full translate-x-0 flex-col rounded-none border-0 shadow-none sm:max-w-full"
          : "top-1/4 sm:max-w-2xl"
      }
      // Inline, so it also wins over the iOS app's centering of dialogs on the visible viewport.
      contentStyle={isMobile ? MOBILE_SHEET_STYLE : undefined}
      label={pageCopy?.label ?? label}
      heading={childPage && pageHeading ? { label: pageHeading } : undefined}
      onBack={childPage ? back : undefined}
      backLabel="Back"
      summary={pageStatus !== null ? pageStatusSummary(pageStatus) : undefined}
      // Commands picked lately lead the commands, under Recent; sessions keep their own order.
      historyKey={sessionsOnly ? undefined : COMMAND_HISTORY_KEY}
      // Sessions lead, unless the query names a command from a word's start, such as "settings".
      sourcesFirst
      placeholder={
        pageCopy?.placeholder ??
        (sessionsOnly ? "Search sessions by name…" : "Search sessions or run a command")
      }
      segments={segments}
      query={childPage ? pageQuery : query}
      onQueryChange={(next) => {
        setParkedId(null);
        settingPage.clearNotice();
        if (childPage) setPageQuery(next);
        else search.setQuery(next);
      }}
      onAccept={accept}
      activeId={parkedId}
      onActiveIdChange={() => setParkedId(null)}
      inputTestId="command-palette-input"
      emptyMessage={childPage ? (optionsPage.empty ?? pageCopy?.empty) : rootEmptyMessage()}
      status={childPage ? undefined : status}
      listEnd={childPage ? undefined : olderSessions}
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
  );
}
