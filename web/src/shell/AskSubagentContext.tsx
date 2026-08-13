// Cross-surface opener for the "Ask sub-agent" dialog. AppShell owns the dialog
// and its pending-selection state; this context lets ChatPage's selection
// toolbar open it with the highlighted text. Mirrors ForkDialogContext.

import { createContext, useContext } from "react";

/** What the selection toolbar captures for an "Ask sub-agent". */
export interface AskSubagentSelection {
  /** The exact text the user highlighted. */
  selectedText: string;
  /**
   * Nearest containing block (paragraph / list item / blockquote / heading /
   * code block) within the same assistant response, capped at 2000 chars — or
   * ``null`` when it adds nothing (absent, empty, or identical to the
   * selection).
   */
  surroundingExcerpt: string | null;
}

export interface AskSubagentContextValue {
  /**
   * Whether the viewer may start a sub-agent here — mirrors the Agents-rail
   * "Add agent" gate (edit access to the source session). The selection
   * toolbar hides "Ask sub-agent" when false.
   */
  canAsk: boolean;
  /**
   * Open the Add Agent dialog prefilled with the selected text (and its
   * surrounding excerpt) as the sub-agent's context.
   */
  askSubagent: (selection: AskSubagentSelection) => void;
}

const AskSubagentContext = createContext<AskSubagentContextValue | null>(null);

export const AskSubagentContextProvider = AskSubagentContext.Provider;

/**
 * Hook for descendants of AppShell. Returns `null` outside the provider (e.g.
 * isolated component tests), so callers hide the affordance when `null`.
 */
export function useAskSubagent(): AskSubagentContextValue | null {
  return useContext(AskSubagentContext);
}
