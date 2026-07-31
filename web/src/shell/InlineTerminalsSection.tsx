// Shells tab content for the right-side rail: the session's shells as
// rows. Clicking a shell row hands it to `onExpand`, which opens the
// shell as a rail tab. Creating a new shell is done from the tab strip's
// "+" menu (see NewTabMenu), not from here. The rail stays a lightweight
// index.

import { TerminalIcon } from "lucide-react";
import { useMemo } from "react";
import { inventoryTerminals, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface InlineTerminalsSectionProps {
  conversationId: string;
  /** Open a shell in the main view, keyed by its terminal tab key. */
  onExpand: (terminalKey: string) => void;
}

export function InlineTerminalsSection({ conversationId, onExpand }: InlineTerminalsSectionProps) {
  const { terminals: allTerminals } = useTerminals(conversationId);
  // Inventory view: the agent's own terminal (SDK REPL / native vendor
  // pane) backs the pill's Terminal view and must not appear as a
  // shell row here.
  const terminalFirstCtx = useTerminalFirst();
  const terminals = useMemo(
    () => inventoryTerminals(allTerminals, terminalFirstCtx?.isTerminalFirst ?? false),
    [allTerminals, terminalFirstCtx?.isTerminalFirst],
  );
  const { getStatus } = useTerminalStatuses(terminals);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      {/* Plain top-aligned list of the session's shells. New shells are
          created from the tab strip's "+" menu, not here — so an empty list
          shows nothing (the "+" is the entry point). */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
        {terminals.map((t) => (
          <button
            key={terminalTabKey(t)}
            type="button"
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-accent/60"
            onClick={() => onExpand(terminalTabKey(t))}
          >
            <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
            {t.session && <span className="shrink-0 text-xs font-medium">{t.session}</span>}
            <span className="truncate text-xs text-muted-foreground/70">{t.name}</span>
            <span className="flex-1" />
            <TerminalStatusBadge status={getStatus(t)} />
          </button>
        ))}
      </div>
    </div>
  );
}
