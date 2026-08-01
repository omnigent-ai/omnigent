// Shells tab content for the right-side rail: the session's shells as
// rows. Clicking a shell row hands it to `onExpand`, which opens the
// shell as a rail tab. On desktop, creating a new shell is done from the
// tab strip's "+" menu (see NewTabMenu), so no create affordance shows
// here. On mobile (no tab strip) the drawer passes ``showNewShell`` to
// surface a leading "+ New shell" row as the create entry point.

import { TerminalIcon } from "lucide-react";
import { useMemo } from "react";
import { inventoryTerminals, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { NewTerminalButton } from "./NewTerminalButton";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface InlineTerminalsSectionProps {
  conversationId: string;
  /** Open a shell in the main view, keyed by its terminal tab key. */
  onExpand: (terminalKey: string) => void;
  /** Show a leading "+ New shell" create row. Off by default — the desktop
   *  rail creates shells from the tab strip's "+" menu. Set on mobile, where
   *  there's no tab strip, so the drawer stays a usable create entry point. */
  showNewShell?: boolean;
}

export function InlineTerminalsSection({
  conversationId,
  onExpand,
  showNewShell = false,
}: InlineTerminalsSectionProps) {
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
      {/* Plain top-aligned list of the session's shells. A leading
          "+ New shell" row (mobile only, gated inside NewTerminalButton on the
          agent's terminal access) keeps the create entry point reachable where
          there's no tab strip "+" menu. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
        {showNewShell && (
          <NewTerminalButton conversationId={conversationId} onCreated={onExpand} variant="row" />
        )}
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
