// The sidebar's terminal panes for one session, in one host for every terminal tab: the selected
// tab's shell, or its split, shows; other mounted shells stay hidden so their terminals survive
// switching tabs. A terminal the main view owns is never mounted here too. Splits use the same
// commands, keys and menus as the main view's panes.

import { useEffect, useLayoutEffect, useMemo, useRef } from "react";

import { useTerminalPaneHotkeys } from "@/hooks/useTerminalPaneHotkeys";
import { terminalDisplayLabel, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { TERMINAL_PANE, TERMINAL_SURFACE } from "@/lib/paneSurfaces";
import {
  groupKeys,
  groupOfTerminal,
  normalizeTerminalGroups,
  type TerminalGroup,
} from "@/lib/terminalGroups";

import { MissingTerminalPane } from "./MissingTerminalPane";
import { usePanesSplitsAllowed } from "./panesViewGate";
import { PaneCloseDialog } from "./PaneCloseDialog";
import { applyDividerResizes } from "./paneDividerCorners";
import { moveSplitPane } from "./paneMove";
import { domCellSizing } from "./paneSizing";
import { giveNextPaneTheKeyboard } from "./paneShellClose";
import { clearPaneStartFailure, isFailedPaneStart } from "./paneStartFailures";
import { type SessionPaneMenu, sessionPaneStrip } from "./paneStrip/paneMenu";
import { sidebarTargetFor } from "./sidebarSplitTarget";
import { shownPanesOf, SplitPanesLayout, useSplitPaneFocusGuards } from "./SplitPanesLayout";
import { focusSplitPane } from "./splitTargetActions";
import { TerminalPaneSurface } from "./TerminalPaneSurface";
import {
  requestTerminalFocus,
  settleTerminalFocus,
  TERMINAL_FOCUS_SURFACE_ATTRIBUTE,
  terminalFocusSurfaceId,
  usePendingTerminalFocus,
} from "./terminalFocusRequest";
import { readTerminalGroups, updateTerminalGroups, useTerminalGroups } from "./terminalGroupsStore";
import { currentTerminalListGeneration, useTerminalListGeneration } from "./terminalListGeneration";
import { usePaneKeyboardOwner } from "./usePaneKeyboardOwner";
import { useTerminalPaneActions } from "./terminalPaneActions";
import { unzoomPane, useZoomedPaneKey } from "./terminalPaneZoom";
import { NO_MAIN_SPLIT_MEMBERS, useMainOwnedTerminalKeys } from "./terminalPlacement";
import { useTerminalStatuses } from "./useTerminalStatuses";

export interface SidebarTerminalPanesProps {
  conversationId: string;
  /** The selected terminal tab's shell, or null while another kind of tab shows. */
  shownKey: string | null;
  /** Every shell to keep mounted, hidden unless shown; the shown one is always included. */
  mountedKeys: readonly string[];
  readOnly: boolean;
  /** The sidebar shows and the selected tab is a terminal. */
  visible: boolean;
  /** A shell the person just opened by hand, whose terminal may take focus as it connects. */
  autoFocusKey: string | null;
  /** Focus moved to another pane of the shown split: its tab becomes the selected one. */
  onFocusShell?: (shellKey: string) => void;
  /** The shells of the main view's split where that view shows it; they never mount here. */
  mainSplitMembers?: ReadonlySet<string>;
}

/** The sidebar's shell groups as it shows them, and the shells the main view owns. */
export function useShownTerminalGroups(
  conversationId: string,
  mainSplitMembers: ReadonlySet<string> = NO_MAIN_SPLIT_MEMBERS,
) {
  const storedGroups = useTerminalGroups(conversationId);
  const mainOwned = useMainOwnedTerminalKeys(conversationId, { splitMembers: mainSplitMembers });
  const mainKeys = [...mainOwned];
  // Placed as it renders, so a group with a main-owned shell (restored, or from another window)
  // never shows it here, even before its saved copy is fixed.
  const groups = normalizeTerminalGroups(storedGroups, { mainKeys, isGone: () => false });
  return { storedGroups, mainOwned, mainKeys, groups };
}

/** The shells that mount with `key`'s when its tab shows: its split's members, or it alone. */
export function sidebarShellUnit(
  groups: readonly TerminalGroup[],
  splitAllowed: boolean,
  key: string,
): readonly string[] {
  const group = splitAllowed ? groupOfTerminal(groups, key) : undefined;
  return group ? groupKeys(group) : [key];
}

export function SidebarTerminalPanes({
  conversationId,
  shownKey,
  mountedKeys,
  readOnly,
  visible,
  autoFocusKey,
  onFocusShell,
  mainSplitMembers = NO_MAIN_SPLIT_MEMBERS,
}: SidebarTerminalPanesProps) {
  const { terminals, listCompleteAt, isFetching } = useTerminals(conversationId);
  const listGeneration = useTerminalListGeneration(conversationId, terminals.length);
  const { setTerminalConnectionState, markTerminalActive } = useTerminalStatuses(terminals);
  // Splits are managed only by desktop owners; everyone else sees one shell per tab.
  const splitAllowed = usePanesSplitsAllowed(readOnly);
  const { storedGroups, mainOwned, mainKeys, groups } = useShownTerminalGroups(
    conversationId,
    mainSplitMembers,
  );
  const storedGroup =
    splitAllowed && shownKey !== null ? groupOfTerminal(groups, shownKey) : undefined;
  // Whichever moved last wins: selecting a member (its tab, Back) focuses that pane; a focus move
  // inside the split (a new pane, a pane key) keeps its pane.
  const storedSplit = storedGroup?.split;
  const seenShownKey = useRef<string | null | undefined>(undefined);
  const selectionMoved = seenShownKey.current !== shownKey;
  const pendingFocus = usePendingTerminalFocus(conversationId, "rail");
  const requestedMember =
    pendingFocus?.tabKey &&
    visible &&
    shownPanesOf(storedSplit, shownKey).includes(pendingFocus.tabKey)
      ? pendingFocus.tabKey
      : null;
  const focusedMember =
    requestedMember ??
    (storedSplit && shownKey !== null && selectionMoved ? shownKey : storedSplit?.focusedKey);
  const split = useMemo(
    () =>
      storedSplit && focusedMember !== undefined && storedSplit.focusedKey !== focusedMember
        ? { ...storedSplit, focusedKey: focusedMember }
        : storedSplit,
    [storedSplit, focusedMember],
  );
  const panesRef = useRef<HTMLDivElement>(null);

  // Saved groups follow: the main view's terminals leave them, and a shell that has ended goes.
  const liveKeys = terminals.map(terminalTabKey).join("\n");
  const listComplete = !isFetching;
  const mainKeysJoined = mainKeys.join("\n");
  const keyboardOwner = usePaneKeyboardOwner(panesRef);
  const shownGroupId = storedGroup?.id ?? null;
  useEffect(() => {
    const live = new Set(liveKeys ? liveKeys.split("\n") : []);
    // A failed start has no tab of its own: it stays only while its group has a running shell.
    const keptFailedStart = (current: readonly TerminalGroup[], key: string) =>
      isFailedPaneStart(conversationId, key) &&
      groupKeys(groupOfTerminal(current, key)!).some(
        (member) => member !== key && live.has(member),
      );
    const mainKeysNow = mainKeysJoined ? mainKeysJoined.split("\n") : [];
    // Only a complete answer newer than the list's last empty one says which shells ended.
    const generation = currentTerminalListGeneration(listGeneration, conversationId, live.size);
    const complete = listComplete && (listCompleteAt ?? 0) > generation.startedAt;
    const before = readTerminalGroups(conversationId);
    const owner = keyboardOwner();
    updateTerminalGroups(conversationId, (current) =>
      normalizeTerminalGroups(current, {
        mainKeys: mainKeysNow,
        // Ended shells drop out only once the list is known to be whole.
        isGone: (key) => complete && !live.has(key) && !keptFailedStart(current, key),
      }),
    );
    // A group that went with its last shell takes its failed starts along; one the main view
    // took keeps them there.
    const after = new Set([
      ...readTerminalGroups(conversationId).flatMap(groupKeys),
      ...mainKeysNow,
    ]);
    for (const key of before.flatMap(groupKeys)) {
      if (!after.has(key)) clearPaneStartFailure(conversationId, key);
    }
    // The pane with the keyboard lost its shell: the pane beside it takes the keyboard.
    if (owner === null || live.has(owner) || after.has(owner)) return;
    // Only a split shown here: a saved split shown as separate tabs leaves it to the tab history.
    const ownerGroup = groupOfTerminal(before, owner);
    if (ownerGroup === undefined || ownerGroup.id !== shownGroupId) return;
    // The split may go with it, leaving its other shell on its own.
    const survivor = groupKeys(ownerGroup).find(
      (key) => key !== owner && live.has(key) && !mainKeysNow.includes(key),
    );
    if (survivor === undefined) return;
    const survivorGroup = groupOfTerminal(readTerminalGroups(conversationId), survivor);
    giveNextPaneTheKeyboard(
      sidebarTargetFor(conversationId, survivor),
      survivorGroup?.split.focusedKey ?? survivor,
    );
  }, [
    conversationId,
    liveKeys,
    listComplete,
    listCompleteAt,
    listGeneration,
    mainKeysJoined,
    storedGroups,
    keyboardOwner,
    shownGroupId,
  ]);

  const target = sidebarTargetFor(conversationId, shownKey ?? "");
  // Save a newly selected member as the focused pane, as the person moving there would.
  const savedFocus = storedSplit?.focusedKey;
  useLayoutEffect(() => {
    seenShownKey.current = shownKey;
    if (requestedMember !== null && savedFocus !== undefined && savedFocus !== requestedMember) {
      focusSplitPane(target, requestedMember);
      return;
    }
    if (
      selectionMoved &&
      shownKey !== null &&
      savedFocus !== undefined &&
      savedFocus !== shownKey
    ) {
      focusSplitPane(target, shownKey);
    }
  }, [target, shownKey, savedFocus, selectionMoved, requestedMember]);
  const actions = useTerminalPaneActions(target, terminals, {
    measureSizing: () => domCellSizing(panesRef.current),
  });
  const paneName = (key: string) => {
    const terminal = terminals.find((candidate) => terminalTabKey(candidate) === key);
    return terminal ? terminalDisplayLabel(terminal) : key;
  };
  const paneCommands = useTerminalPaneHotkeys({
    target,
    enabled: splitAllowed && visible && shownKey !== null,
    split,
    singlePaneKey: shownKey,
    actions,
    panesRef,
    paneName,
  });
  const zoomedKey = useZoomedPaneKey(target, split);
  const { takeConnectFocus, focusStillHere } = useSplitPaneFocusGuards({
    conversationId,
    visible,
    split,
    shownPaneKeys: shownPanesOf(split, shownKey),
    panesRef,
  });
  // A shell opened by hand takes the keyboard as it connects while focus stays where the open
  // left it, such as in the chat composer; moving focus meanwhile cancels that.
  const launchFocus = useRef<{ key: string; element: Element | null } | null>(null);
  useLayoutEffect(() => {
    launchFocus.current =
      autoFocusKey === null ? null : { key: autoFocusKey, element: document.activeElement };
  }, [autoFocusKey]);
  const focusUnmovedSinceOpen = (paneKey: string) =>
    launchFocus.current?.key === paneKey && launchFocus.current.element === document.activeElement;

  // The pane a request goes to: the one it names, or the focused one for the view's own.
  const focusRecipient =
    !pendingFocus || !visible || shownKey === null
      ? null
      : pendingFocus.tabKey === null
        ? (split?.focusedKey ?? shownKey)
        : requestedMember;
  // Focus the person moves anywhere else here, such as into Find, cancels the request.
  const hostRef = useRef<HTMLDivElement>(null);
  const latestRecipient = useRef(focusRecipient);
  latestRecipient.current = focusRecipient;
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const cancelElsewhere = (event: FocusEvent) => {
      if (!(event.target instanceof Element) || latestRecipient.current === null) return;
      const pane = event.target.closest<HTMLElement>(TERMINAL_PANE)?.dataset.terminalPane;
      const inRecipient =
        pane === latestRecipient.current && event.target.closest(TERMINAL_SURFACE) !== null;
      if (!inRecipient) settleTerminalFocus(conversationId, "rail");
    };
    host.addEventListener("focusin", cancelElsewhere);
    return () => host.removeEventListener("focusin", cancelElsewhere);
  }, [conversationId]);

  const focusPane = (key: string) => {
    focusSplitPane(target, key);
    // A failed start's slot has no tab to select; the split stays shown through another's.
    if (!isFailedPaneStart(conversationId, key)) onFocusShell?.(key);
  };
  const requestPaneFocus = (key: string) =>
    requestTerminalFocus({ conversationId, surface: "rail", tabKey: key });
  const paneMenuFor = (paneKey: string): SessionPaneMenu | undefined =>
    splitAllowed && actions.shellOptions.length > 0
      ? {
          onSplit: (direction) =>
            actions.splitPane(paneKey, direction === "right" ? "columns" : "rows"),
          splitting: actions.splitting || actions.closingSplit,
          commands: () => paneCommands(paneKey),
        }
      : undefined;
  const mounted = [...new Set([...(shownKey ? [shownKey] : []), ...mountedKeys])].filter(
    (key) => key === shownKey || !mainOwned.has(key),
  );
  if (mounted.length === 0) return null;

  return (
    <div
      ref={hostRef}
      className="flex h-full min-h-0 flex-col"
      {...{ [TERMINAL_FOCUS_SURFACE_ATTRIBUTE]: terminalFocusSurfaceId(conversationId, "rail") }}
    >
      <SplitPanesLayout
        conversationId={conversationId}
        split={split}
        singlePaneKey={shownKey}
        alsoMounted={mounted}
        paneIdOf={(key) => terminals.find((t) => terminalTabKey(t) === key)?.id ?? key}
        onFocusPane={focusPane}
        onResize={(resizes) =>
          target.update((current) =>
            current
              ? { ...current, layout: applyDividerResizes(current.layout, resizes) }
              : current,
          )
        }
        panesRef={panesRef}
        zoomedKey={zoomedKey}
        onUnzoom={(key) => {
          unzoomPane(target);
          requestPaneFocus(key);
        }}
        onMovePane={
          splitAllowed && visible
            ? (move) => {
                moveSplitPane(target, move, domCellSizing(panesRef.current));
                requestPaneFocus(move.key);
              }
            : undefined
        }
        refocusPane={requestPaneFocus}
        strip={{
          paneOf: (paneKey) => {
            if (mainOwned.has(paneKey)) return null;
            const terminal = terminals.find((t) => terminalTabKey(t) === paneKey);
            return terminal
              ? sessionPaneStrip(terminalDisplayLabel(terminal), paneMenuFor(paneKey), {
                  focus: () => requestPaneFocus(paneKey),
                })
              : null;
          },
        }}
        renderPane={(paneKey, focused, shown) => {
          if (mainOwned.has(paneKey)) {
            return shown ? (
              <div className="flex flex-1 items-center justify-center px-4 text-center text-muted-foreground text-ui">
                This shell is open in the Terminal view.
              </div>
            ) : null;
          }
          const terminal = terminals.find((t) => terminalTabKey(t) === paneKey);
          if (!terminal) {
            return shown ? (
              <MissingTerminalPane
                target={target}
                paneKey={paneKey}
                // Still selected just as its start fails, it hands the split to another member.
                focused={visible && focused && paneKey !== shownKey}
                terminals={terminals}
                readOnly={readOnly}
                fallback="Shell not available."
              />
            ) : null;
          }
          const takesFocus = shown && visible && focused;
          return (
            <TerminalPaneSurface
              conversationId={conversationId}
              terminal={terminal}
              readOnly={readOnly}
              active={takesFocus}
              focusOnConnect={() =>
                takesFocus &&
                (paneKey === autoFocusKey
                  ? focusUnmovedSinceOpen(paneKey) || focusStillHere()
                  : split !== undefined && takeConnectFocus(paneKey) && focusStillHere())
              }
              focusRequest={
                pendingFocus && focusRecipient === paneKey ? pendingFocus.id : undefined
              }
              onFocusRequestTaken={(id) => settleTerminalFocus(conversationId, "rail", id)}
              onStateChange={(state) => setTerminalConnectionState(terminal.id, state)}
              onActivity={() => markTerminalActive(terminal.id)}
            />
          );
        }}
      />
      {splitAllowed && <PaneCloseDialog actions={actions} />}
    </div>
  );
}
