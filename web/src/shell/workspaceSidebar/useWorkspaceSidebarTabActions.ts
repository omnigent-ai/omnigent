// What every workspace sidebar does with its tabs, whoever hosts it: close, close others,
// reopen, move and step, from the strip, its menus, ⌘K and the chords, with focus kept in
// the sidebar after a keyboard close. A host adds only what its tabs need, such as the
// session's shells closing as one split.

import type { KeyboardEvent } from "react";

import { matchesShortcut, type ShortcutId } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { isCommandAvailable } from "@/store/paletteCommands";

import {
  tabActionCommandFor,
  type WorkspaceTabAction,
  type WorkspaceTabActionCommand,
  type WorkspaceTabGroupCloseAction,
  type WorkspaceTabListFacts,
  workspaceTabActionCommands,
} from "../workspaceCommands/model";
import {
  useWorkspaceTabChords,
  type WorkspaceTabRunOrigin,
} from "../workspaceCommands/useWorkspaceTabChords";
import {
  isFocusInWorkspaceSidebar,
  keepSidebarFocusAfterClose,
} from "../workspaceTabs/keepSidebarFocus";
import type { TabStripWidth } from "../workspaceTabs/useTabStripWidth";
import { isPinnedTab } from "../workspaceTabs/pinnedTabs";
import { groupClosesOf, type TabGroupId } from "../workspaceTabs/tabGroups";
import { useWorkspaceTabStepping } from "../workspaceTabs/useWorkspaceTabStepping";
import type { StripTabMenuItem } from "../workspaceTabs/WorkspaceStripTab";
import type { TabId } from "../workspaceTabs/workspacePlace";
import { isUnknownTab, type WorkspaceTab } from "../workspaceTabs/workspaceTab";
import {
  isBatchClosable,
  isOnlyEmptyNewTab,
  type WorkspaceTabsStore,
} from "../workspaceTabs/workspaceTabsStore";

/** Chords only the focused strip handles, so their ⌘K rows carry none; the menu still shows them. */
const STRIP_TAB_SHORTCUTS: Partial<Record<WorkspaceTabAction, ShortcutId>> = {
  "move-left": "workspace.moveTabLeft",
  "move-right": "workspace.moveTabRight",
};

/** How a tab's close treats its resource, as `WorkspaceTabsStore.close` takes it. */
export type TabCloseSteps = Parameters<WorkspaceTabsStore["close"]>[1];

export interface WorkspaceSidebarTabActionsOptions {
  store: WorkspaceTabsStore;
  /** Scopes the ⌘K rows and binds the chords; null binds nothing, such as while starting. */
  scopeId: string | null;
  stripWidth: TabStripWidth;
  /** The sidebar is on screen; the strip's actions wait for it. */
  sidebarOpen: boolean;
  /** The strip's entries in order, which Previous and Next Sidebar Tab step through. */
  entryIds: readonly TabId[];
  /** The entry the strip shows selected. */
  selectedEntryId: TabId | null;
  /** The selected tab's name, so Close names what it closes. */
  selectedLabel?: string;
  /** Why a tab can't close; absent when it can. */
  closeRefusalFor?: (tabId: TabId) => string | undefined;
  /** Whether this user may close `tab` at all, such as a shell without edit access. */
  canClose?: (tab: WorkspaceTab) => boolean;
  /** How closing `tab` treats its resource, such as a shell ending first. */
  closeStepsFor?: (tab: WorkspaceTab) => TabCloseSteps;
  /** Releases what closing others leaves behind, such as a browser view. */
  releaseFor?: (tab: WorkspaceTab) => (() => Promise<boolean>) | undefined;
  /**
   * Closes the strip entry `tabId` stands for when the host shows several tabs as one, such as
   * a split, returning the close; null to close it as a tab. Used by Close tab and its chord.
   */
  closeEntry?: (tabId: TabId, source?: "pointer") => Promise<{ ok: boolean }> | null;
  /**
   * Moves the strip entry `tabId` is in one entry over, with every tab it stands for, where the
   * host shows several tabs as one; true once handled, even with no entry to move past, and
   * false to move it as a tab. Used by Move Tab Left and Right, their chords and the menu.
   */
  moveEntry?: (tabId: TabId, delta: -1 | 1) => boolean;
  /** The actions only some hosts offer. */
  offers?: { newTab?: () => void; searchTabs?: () => void };
  /** Offers Pin and Unpin, for a strip that shows pinned tabs; `refusalFor` says why a tab can't. */
  pinning?: { refusalFor?: (tabId: TabId) => string | undefined };
  /**
   * Offers closing a group of tabs at once, for a tab list that groups them: the group each
   * tab is listed under, and whether a pull request tab's PR has merged.
   */
  tabGroups?: {
    groupOf: (tab: WorkspaceTab) => TabGroupId;
    merged: (tab: WorkspaceTab) => boolean;
  };
  /** Puts the sidebar on screen, as reopening a tab does. */
  onReveal?: () => void;
}

function focusStripTab(tabId: TabId) {
  document.querySelector<HTMLElement>(`[data-workspace-tab-id="${tabId}"]`)?.focus();
}

export function useWorkspaceSidebarTabActions(options: WorkspaceSidebarTabActionsOptions) {
  const {
    store,
    scopeId,
    stripWidth,
    sidebarOpen,
    entryIds,
    selectedEntryId,
    selectedLabel,
    closeRefusalFor,
    canClose = () => true,
    closeStepsFor,
    releaseFor,
    closeEntry,
    moveEntry,
    offers = {},
    pinning,
    tabGroups,
    onReveal,
  } = options;
  const { tabs, selectedTabId, canReopen } = store.snapshot();

  // A close by the × or a middle click holds tab widths while the pointer stays on the strip.
  // `fromSidebar` is what the ⌘W that asked for the close knew, where the page's focus is stale.
  const closeTab = (tabId: TabId, source?: "pointer", fromSidebar?: boolean) => {
    if (source === "pointer") stripWidth.closedByPointer();
    else stripWidth.closedByKeyboard();
    const tab = store.findTab(tabId);
    if (tab && !canClose(tab)) return;
    const closing = store.close(tabId, tab && closeStepsFor ? closeStepsFor(tab) : {});
    keepFocusAfterKeyboardClose(closing, source, fromSidebar);
  };
  // A pointer close keeps the strip still under the pointer, so only a keyboard close moves focus.
  const keepFocusAfterKeyboardClose = (
    closing: Promise<{ ok: boolean }>,
    source: "pointer" | undefined,
    fromSidebar: boolean | undefined,
  ) => {
    if (source === "pointer") return;
    void keepSidebarFocusAfterClose(
      closing,
      () => store.snapshot().selectedTabId,
      fromSidebar ?? isFocusInWorkspaceSidebar(),
    );
  };
  // The tab the strip shows for `tabId`: a split closes as one, keeping focus as a tab's close does.
  const closeVisibleTab = (tabId: TabId, source?: "pointer", fromSidebar?: boolean) => {
    const inSidebar = fromSidebar ?? isFocusInWorkspaceSidebar();
    const closing = closeEntry?.(tabId, source) ?? null;
    if (closing === null) closeTab(tabId, source, inSidebar);
    else keepFocusAfterKeyboardClose(closing, source, inSidebar);
  };
  // Shells stay: closing others shouldn't end every shell with a dialog each.
  const closeOtherTabs = (keepTabId: TabId) => {
    void store.closeOthers(keepTabId, releaseFor ?? (() => undefined));
  };
  const moveTab = (tabId: TabId, delta: -1 | 1) => {
    if (moveEntry?.(tabId, delta)) return;
    const index = store.tabs().findIndex((tab) => tab.id === tabId);
    if (index !== -1) store.move(tabId, index + delta);
  };
  const togglePin = (tabId: TabId) => {
    const tab = store.findTab(tabId);
    if (!tab || pinning?.refusalFor?.(tabId)) return;
    store.setPinned([tabId], !isPinnedTab(tab));
    // The tab moves to the other group and mounts again there, taking the focus it or its
    // menu had along with it; give it back, unless something else has taken it since.
    requestAnimationFrame(() => {
      const focused = document.activeElement;
      if (focused && focused !== document.body && focused.isConnected) return;
      focusStripTab(tabId);
    });
  };
  const reopenClosedTab = () => {
    if (store.reopenClosed() !== null) onReveal?.();
  };
  // What each group close takes: the tabs shown here that this user may close and that close
  // as a group, so pinned tabs and shells stay.
  const groupCloseTargets = (() => {
    if (!tabGroups) return undefined;
    const targets: Record<WorkspaceTabGroupCloseAction, TabId[]> = {
      "close-merged-pull-requests": [],
      "close-pull-request-tabs": [],
      "close-file-tabs": [],
      "close-page-tabs": [],
    };
    for (const tab of tabs) {
      if (isUnknownTab(tab) || !isBatchClosable(tab) || !canClose(tab)) continue;
      for (const action of groupClosesOf(tabGroups.groupOf(tab), tabGroups.merged(tab))) {
        targets[action].push(tab.id);
      }
    }
    return targets;
  })();
  const closeGroup = (action: WorkspaceTabGroupCloseAction) => {
    const targetIds = groupCloseTargets?.[action];
    if (!targetIds?.length) return;
    void store.closeMany(targetIds, releaseFor ?? (() => undefined));
  };

  // Moves and Close Others go by the strip's entries, a split being one.
  const selectedIndex = selectedEntryId === null ? -1 : entryIds.indexOf(selectedEntryId);
  const selectedTab = selectedTabId === null ? undefined : store.findTab(selectedTabId);
  const facts: WorkspaceTabListFacts = {
    canReopen,
    pinnedCount: tabs.filter(isPinnedTab).length,
    selectedPinned: selectedTab !== undefined && isPinnedTab(selectedTab),
    selectedPinRefusal: selectedTabId === null ? undefined : pinning?.refusalFor?.(selectedTabId),
    selectedIndex,
    tabCount: entryIds.length,
    sidebarOpen,
    selectedCloseRefusal: selectedTabId === null ? undefined : closeRefusalFor?.(selectedTabId),
    selectedIsOnlyEmptyNewTab: selectedTabId !== null && isOnlyEmptyNewTab(tabs, selectedTabId),
    selectedLabel,
    ...(groupCloseTargets
      ? {
          groupCloseCounts: Object.fromEntries(
            Object.entries(groupCloseTargets).map(([action, ids]) => [action, ids.length]),
          ) as Record<WorkspaceTabGroupCloseAction, number>,
        }
      : {}),
  };
  const commands = workspaceTabActionCommands(facts).filter(
    (command) =>
      (command.action !== "new-tab" || offers.newTab !== undefined) &&
      (command.action !== "search-tabs" || offers.searchTabs !== undefined) &&
      (command.action !== "toggle-pin" || pinning !== undefined),
  );
  // A tab's menu shows the chord of the command each row matches, from the registry.
  const shortcutFor = (action: WorkspaceTabAction): ShortcutId | undefined =>
    commands.find((command) => command.action === action)?.shortcut ?? STRIP_TAB_SHORTCUTS[action];

  const run = (command: WorkspaceTabActionCommand, origin?: WorkspaceTabRunOrigin) => {
    if (!isCommandAvailable(command.availability)) return;
    switch (command.action) {
      case "new-tab":
        offers.newTab?.();
        return;
      case "close-tab":
        if (selectedTabId !== null) {
          closeVisibleTab(selectedTabId, undefined, origin?.fromWorkspaceSidebar);
        }
        return;
      case "reopen":
        reopenClosedTab();
        return;
      case "close-others":
        if (selectedTabId !== null) closeOtherTabs(selectedTabId);
        return;
      case "move-left":
        if (selectedTabId !== null) moveTab(selectedTabId, -1);
        return;
      case "move-right":
        if (selectedTabId !== null) moveTab(selectedTabId, 1);
        return;
      case "toggle-pin":
        if (selectedTabId !== null) togglePin(selectedTabId);
        return;
      case "search-tabs":
        offers.searchTabs?.();
        return;
      case "close-merged-pull-requests":
      case "close-pull-request-tabs":
      case "close-file-tabs":
      case "close-page-tabs":
        closeGroup(command.action);
        return;
    }
  };

  // New, Close and Reopen also have chords, which work with the sidebar closed too.
  useWorkspaceTabChords({ conversationId: scopeId, commands, facts, run });
  // ⌘⇧[ / ⌘⇧] step the strip's entries.
  useWorkspaceTabStepping({
    entryIds,
    selectedId: selectedEntryId,
    onSelect: (tabId) => {
      store.select(tabId);
      onReveal?.();
    },
    enabled: scopeId !== null,
  });

  // ── Strip keyboard: ⌥⇧←/→ moves the focused tab ──
  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, tabId: TabId) => {
    const left = matchesShortcut(event.nativeEvent, "workspace.moveTabLeft");
    if (!left && !matchesShortcut(event.nativeEvent, "workspace.moveTabRight")) return;
    event.preventDefault();
    event.stopPropagation();
    moveTab(tabId, left ? -1 : 1);
    requestAnimationFrame(() => focusStripTab(tabId));
  };

  /** The menu every ordinary tab shows: close it or the others, move it, reopen one. */
  const menuItemsFor = (tabId: TabId, label: string, closable: boolean): StripTabMenuItem[] => {
    // This tab's own commands, so its rows read as the menus and ⌘K do, and Close names it
    // even while another tab is selected.
    const rowTab = store.findTab(tabId);
    const rowCommands = workspaceTabActionCommands({
      ...facts,
      // Its place among the strip's entries, which Move Left and Right go by, a split being one.
      selectedIndex: entryIds.indexOf(tabId),
      selectedLabel: label,
      selectedPinned: rowTab !== undefined && isPinnedTab(rowTab),
      selectedPinRefusal: pinning?.refusalFor?.(tabId),
    });
    const rowCommand = (action: WorkspaceTabAction) => tabActionCommandFor(rowCommands, action);
    const menuLabel = (action: WorkspaceTabAction) =>
      tabActionCommandFor(rowCommands, action).menuLabel;
    return [
      {
        id: "close",
        label: menuLabel("close-tab"),
        disabled: !closable,
        shortcut: shortcutFor("close-tab"),
        // From the tab's own menu, which has focus as it runs: the close is the sidebar's.
        onSelect: () => closeTab(tabId, undefined, true),
      },
      {
        id: "close-others",
        label: menuLabel("close-others"),
        disabled: tabs.length < 2,
        onSelect: () => closeOtherTabs(tabId),
      },
      // Split shells can't be pinned, so their menu has no row for it.
      ...(pinning && !pinning.refusalFor?.(tabId)
        ? [
            {
              id: "toggle-pin",
              label: menuLabel("toggle-pin"),
              disabled: false,
              onSelect: () => togglePin(tabId),
              separatorBefore: true,
            },
          ]
        : []),
      {
        id: "move-left",
        label: menuLabel("move-left"),
        disabled: !isCommandAvailable(rowCommand("move-left").availability),
        shortcut: shortcutFor("move-left"),
        onSelect: () => moveTab(tabId, -1),
        separatorBefore: true,
      },
      {
        id: "move-right",
        label: menuLabel("move-right"),
        disabled: !isCommandAvailable(rowCommand("move-right").availability),
        shortcut: shortcutFor("move-right"),
        onSelect: () => moveTab(tabId, 1),
      },
      // Offered only once a closed tab can come back; ⌘K still says why it can't.
      ...(canReopen
        ? [
            {
              id: "reopen",
              label: menuLabel("reopen"),
              disabled: false,
              shortcut: shortcutFor("reopen"),
              onSelect: reopenClosedTab,
              separatorBefore: true,
            },
          ]
        : []),
    ];
  };

  return {
    commands,
    facts,
    run,
    closeTab,
    closeVisibleTab,
    closeOtherTabs,
    moveTab,
    togglePin,
    reopenClosedTab,
    shortcutFor,
    handleTabKeyDown,
    menuItemsFor,
  };
}

export type WorkspaceSidebarTabActions = ReturnType<typeof useWorkspaceSidebarTabActions>;
