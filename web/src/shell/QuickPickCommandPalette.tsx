// ⌘K on QuickPick: the same Sessions, In messages and Actions rows as CommandPalette,
// with fuzzy-ranked actions and keyboard navigation from the QuickPick list, and the
// commands features on screen offer through `usePaletteCommands`, under their headings.

import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import { XIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  openRecentSessionSwitcher,
  useClaimRecentSessionChord,
} from "@/hooks/useRecentSessionSwitch";
import { Button } from "@/components/ui/button";
import {
  type QuickPickAcceptDetails,
  type QuickPickAcceptResult,
  QuickPickDialog,
  type QuickPickHandOff,
  type QuickPickItem,
  type QuickPickSegment,
} from "@/components/ui/quick-pick";
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
  leavePaletteChildPage,
  type PaletteParentPlace,
  paletteChildPageStore,
  rememberPaletteParent,
  resetPaletteToRoot,
} from "./commandPaletteChildPage";
import { currentPaletteCommand, paletteCommandItems } from "./paletteCommandRows";
import { SESSION_COMMAND_PICKS, type SessionCommandId } from "./sessionCommands/model";
import { useSessionActionsPage } from "./sessionCommands/useSessionActionsPage";
import type { PaletteTarget as PaletteRow } from "./paletteTarget";
import { pageStatusSummary, useSettingChoicesPage } from "./settingChoicesPage";
import { sessionSearchItems, useShowMoreParking } from "./paletteSessionItems";
import { usePaletteSessionSearch } from "./usePaletteSessionSearch";

const COMMAND_HISTORY_KEY = "command-palette:commands";

/** Said when a pasted `#123` or pull request link names no loaded session's pull request. */
export const PULL_REQUEST_NOT_FOUND =
  "No session found for that pull request. Search checks each loaded session's linked pull request.";

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
  "pick-session": {
    label: "Pick a session",
    placeholder: "Pick a session, or paste a pull request link…",
    empty: "No matching sessions.",
  },
  "session-actions": {
    label: "Session actions",
    placeholder: "Choose an action…",
    empty: "No actions for this session.",
  },
} as const;

/** The phone sheet fills the keyboard-aware viewport, clear of the title bar and home bar. */
const MOBILE_SHEET_STYLE: CSSProperties = {
  top: 0,
  height: "var(--omnigent-viewport-height, 100dvh)",
  maxHeight: "var(--omnigent-viewport-height, 100dvh)",
  paddingTop: "var(--mac-titlebar-height, var(--omnigent-safe-top, 0px))",
  paddingBottom: "var(--omnigent-safe-bottom, 0px)",
};

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
  const childPage = useStore(paletteChildPageStore, (state) => state.page);
  // A verb-first row such as "Archive a Session…" lists sessions alone, searched as the root is:
  // loaded matches, the server's title and message matches with their paging, and archived
  // sessions where the action applies to them.
  const picking = childPage?.kind === "pick-session" ? childPage : null;
  const search = usePaletteSessionSearch({
    open,
    sessionsOnly,
    matchLoaded: true,
    includeArchived: picking
      ? (SESSION_COMMAND_PICKS[picking.command]?.includesArchived ?? false)
      : undefined,
  });
  const { query, debouncedQuery } = search;
  const close = (): void => onOpenChange(false);
  // ⌃] / ⌃[ close the palette and open the held recent-session switcher in its place.
  useClaimRecentSessionChord(open, (direction) => {
    close();
    openRecentSessionSwitcher(direction, { holding: true, navigate });
  });
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
  // Back makes the row that opened a page active again; the user's next move clears it.
  const [parkedId, setParkedId] = useState<string | null>(null);
  // A child page has its own query; the root's stays as it was for Back.
  const [pageQuery, setPageQuery] = useState("");
  // The action a picked session's actions page opened for, so the page can say if it doesn't apply.
  const [pickedAction, setPickedAction] = useState<{
    sessionId: string;
    title: string;
    command: SessionCommandId;
  } | null>(null);
  // Every opening starts from the top row of a fresh root, and a close drops what a page awaited.
  useEffect(() => {
    if (!open) setParkedId(null);
    setPickedAction(null);
    resetPaletteToRoot();
  }, [open]);
  // Back on the row that opened the page, once a choice applies or its setting goes away.
  // The root and the session picker filter with the session search; another page has its own.
  const restoreParent = (parent: PaletteParentPlace | null) => {
    const searchesSessions = !parent?.page || parent.page.page.kind === "pick-session";
    setPageQuery(parent && !searchesSessions ? parent.query : "");
    if (parent && searchesSessions && parent.query !== query) search.setQuery(parent.query);
    setParkedId(parent?.activeId ?? null);
  };
  const settingPage = useSettingChoicesPage(commandItems, targetKey, restoreParent);
  const optionsPage = useCommandOptionsPage(
    (itemId, pageTargetKey) => commandFor(itemId, pageTargetKey),
    restoreParent,
  );
  const sessionPage = useSessionActionsPage();

  // The session rows and their paging, from the shared session-search rows.
  const sessionRows = sessionSearchItems(
    search,
    { names: "Sessions", messages: "In messages" },
    { labelQuery: sessionsOnly ? query : debouncedQuery, snippetQuery: debouncedQuery },
  );
  // Picked by kind, so a page kind nothing builds yet lists no rows instead of another page's.
  const activePage = (() => {
    switch (childPage?.kind) {
      case "setting-choices":
        return settingPage;
      case "session-actions":
        return sessionPage;
      case "command-options":
        return optionsPage;
      case "pick-session":
        return {
          items: [...sessionRows.names, ...sessionRows.messages],
          back: leavePaletteChildPage,
        };
      default:
        return null;
    }
  })();
  const pageItems = activePage?.items ?? null;
  const rootSegments: QuickPickSegment<PaletteRow>[] = [
    {
      sourceId: "sessions",
      ranking: "provider",
      origin: "source",
      items: sessionRows.names,
    },
    {
      sourceId: "messages",
      ranking: "provider",
      origin: "source",
      items: sessionRows.messages,
    },
    {
      // One segment for every command, so the best match leads whichever group it's in: an
      // exact name such as "Archive" beats a longer one under an earlier heading.
      sourceId: "commands",
      ranking: "client",
      groupOrder: "bestMatch",
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
            ...commandItems.map((item): QuickPickItem<PaletteRow> => ({
              ...item,
              group: item.group ?? "Actions",
              matchHighlight: "underline",
            })),
          ],
    },
  ];
  // A page kind nothing builds yet lists nothing, rather than the root's rows.
  const unbuiltPage = childPage !== null && activePage === null;
  const segments: QuickPickSegment<PaletteRow>[] = picking
    ? // The session search already matched them, as on the root.
      [{ sourceId: "sessions", ranking: "provider", origin: "source", items: pageItems ?? [] }]
    : pageItems || unbuiltPage
      ? [{ sourceId: "page", ranking: "client", fuzzyMatching: "words", items: pageItems ?? [] }]
      : rootSegments;
  // Show more parks the selection on its group's last row, until the query changes or ⌘K closes.
  const showMore = useShowMoreParking(
    segments.flatMap((segment) => segment.items),
    { query, open },
  );

  const accept = (
    item: QuickPickItem<PaletteRow>,
    { alternate }: QuickPickAcceptDetails,
  ): QuickPickAcceptResult | QuickPickHandOff => {
    const row = item.value;
    if (row.kind === "more") {
      setParkedId(null);
      showMore.accept({ ...item, value: row });
      return "keepOpen";
    }
    showMore.clear();
    if (row.kind === "action") {
      row.action.run();
      return undefined;
    }
    if (row.kind === "setting-choice") return settingPage.acceptChoice(row.value);
    if (row.kind === "command-option") return optionsPage.acceptOption(row.optionId);
    if (row.kind === "session-action") return sessionPage.acceptAction(row.command);
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
        const pageBefore = paletteChildPageStore.getState().page;
        command.run();
        const pageAfter = paletteChildPageStore.getState().page;
        // It opened the session picker: Back returns to this row, and the search starts empty.
        if (pageAfter !== pageBefore && pageAfter?.kind === "pick-session") {
          rememberPaletteParent({ query, activeId: item.id });
          search.setQuery("");
          setParkedId(null);
        }
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
    const { visibleSessions, contentSessions } = search;
    // A picked session opens its actions on the one the verb named, still checked as it runs.
    if (picking && row.kind === "session") {
      const picked = [...visibleSessions, ...contentSessions].find(
        (candidate) => candidate.id === row.id,
      );
      if (!picked) return "keepOpen";
      // Back returns to the picker, on this session and with the query that found it.
      sessionPage.openActions(picked.conversation, picked.label, {
        query,
        activeId: item.id,
        page: { page: picking, parent: paletteChildPageStore.getState().parent },
      });
      setPageQuery("");
      setParkedId(`session-action:${picked.id}:${picking.command}`);
      setPickedAction({ sessionId: picked.id, title: picked.label, command: picking.command });
      return "keepOpen";
    }
    // ⌘↵ on a session lists its actions, with Back returning to this row.
    const session = alternate
      ? [...visibleSessions, ...contentSessions].find((candidate) => candidate.id === row.id)
      : undefined;
    if (session) {
      sessionPage.openActions(session.conversation, session.label, { query, activeId: item.id });
      setPageQuery("");
      setParkedId(null);
      return "keepOpen";
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
  // As in today's palette, a load error or loading message stands in for the empty one; the
  // session picker searches as the root does, so it says the same while it waits.
  const searchEmptyMessage = (nothingFound: string): string | null => {
    if (loadStatus) return null;
    if (search.pullRequestLookup) return PULL_REQUEST_NOT_FOUND;
    return search.searchFetching ? "Searching…" : nothingFound;
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
  const pickedRowId =
    pickedAction && `session-action:${pickedAction.sessionId}:${pickedAction.command}`;
  // The verb's action isn't one this session offers, such as Unpin on a session that isn't pinned.
  const pickNotice =
    pickedAction && sessionPage.open && !sessionPage.items.some((row) => row.id === pickedRowId)
      ? (SESSION_COMMAND_PICKS[pickedAction.command]?.notAvailable(pickedAction.title) ?? null)
      : null;
  const pageStatus = settingPage.open
    ? settingPage.status
    : optionsPage.open
      ? optionsPage.status
      : (pickNotice ?? settingPage.notice);
  const back = () => {
    setPickedAction(null);
    restoreParent(activePage ? activePage.back() : leavePaletteChildPage());
  };
  const pageHeading =
    settingPage.heading ??
    sessionPage.heading ??
    optionsPage.heading ??
    (picking ? (SESSION_COMMAND_PICKS[picking.command]?.question ?? null) : null);

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
      // ⌘↵ on a session row lists that session's actions; phones have no ⌘ to press.
      alternateLabel={
        childPage === null && !isMobile
          ? (item) => (item.value.kind === "session" ? "Show actions" : undefined)
          : undefined
      }
      summary={pageStatus !== null ? pageStatusSummary(pageStatus) : undefined}
      // Commands picked lately lead the commands, under Recent; sessions keep their own order.
      historyKey={sessionsOnly || picking ? undefined : COMMAND_HISTORY_KEY}
      // Sessions lead, unless the query names a command from a word's start, such as "settings".
      sourcesFirst
      placeholder={
        pageCopy?.placeholder ??
        (sessionsOnly ? "Search sessions by name…" : "Search sessions or run a command")
      }
      segments={segments}
      query={childPage && !picking ? pageQuery : query}
      onQueryChange={(next) => {
        setParkedId(null);
        settingPage.clearNotice();
        if (childPage && !picking) setPageQuery(next);
        else search.setQuery(next);
      }}
      onAccept={accept}
      activeId={parkedId ?? showMore.activeId}
      onActiveIdChange={() => {
        setParkedId(null);
        showMore.clear();
      }}
      inputTestId="command-palette-input"
      emptyMessage={
        !childPage
          ? searchEmptyMessage("No results found")
          : picking
            ? searchEmptyMessage(CHILD_PAGE_COPY["pick-session"].empty)
            : (optionsPage.empty ?? pageCopy?.empty)
      }
      status={childPage && !picking ? undefined : status}
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
