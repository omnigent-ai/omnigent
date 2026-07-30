// Shared data hook for InlineTerminalsSection and TerminalsPanel.

import { useEffect, useMemo, useState } from "react";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { inventoryTerminals, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { useTerminalFirst } from "./TerminalFirstContext";
import { useTerminalStatuses } from "./useTerminalStatuses";

// Only the terminal the user actually selects opens a WebSocket (via
// ``TerminalView``). Opening the panel used to fan out a read-only
// ``tmux attach`` to every unselected terminal for status badges; with N
// terminals that meant N simultaneous ``pty.fork`` + ``tmux attach`` on
// the runner, which could take the runner down. Unselected terminals now
// derive their badge from the resource ``running`` flag alone (see
// ``deriveTerminalStatus``).
export function useTerminalSplit(conversationId: string) {
  const { terminals: allTerminals } = useTerminals(conversationId);
  // Inventory view: the agent's own terminal (SDK REPL / native vendor
  // pane) backs the pill's Terminal view and must not appear as a
  // shell row here.
  const terminalFirstCtx = useTerminalFirst();
  const terminals = useMemo(
    () => inventoryTerminals(allTerminals, terminalFirstCtx?.isTerminalFirst ?? false),
    [allTerminals, terminalFirstCtx?.isTerminalFirst],
  );
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const { getStatus, setTerminalConnectionState, markTerminalActive } =
    useTerminalStatuses(terminals);

  const activeTerminal =
    activeKey !== null ? (terminals.find((t) => terminalTabKey(t) === activeKey) ?? null) : null;

  const {
    width: listWidth,
    containerRef: splitRef,
    handleProps: columnHandleProps,
  } = useResizableColumn();

  // Clear activeKey if the selected terminal is removed. Do NOT
  // auto-select — null is the intentional "no terminal selected" state.
  useEffect(() => {
    if (activeKey === null) return;
    if (!terminals.some((t) => terminalTabKey(t) === activeKey)) setActiveKey(null);
  }, [terminals, activeKey]);

  return {
    terminals,
    activeKey,
    setActiveKey,
    activeTerminal,
    getStatus,
    setTerminalConnectionState,
    markTerminalActive,
    listWidth,
    splitRef,
    columnHandleProps,
  };
}
