// Cross-surface opener for the session fork/clone dialog. AppShell owns
// the dialog and its state (open flag + optional truncation point); this
// context lets descendants outside the header — e.g. ChatPage's
// per-message "Fork from here" action — open it with an
// `upToResponseId`, which makes the fork copy history only up to and
// including that response.

import { createContext, useContext } from "react";

export interface ForkDialogContextValue {
  /**
   * Whether the current session can be forked at all — mirrors the
   * header Clone button's gating (top-level session, read access).
   * Callers hide their fork affordance when false.
   */
  canFork: boolean;
  /**
   * Open the fork/clone dialog. With `upToResponseId` set, the dialog
   * submits a truncated fork ("fork from this response"); without it,
   * a full clone (the header button's behavior).
   */
  openForkDialog: (opts?: { upToResponseId?: string }) => void;
}

const ForkDialogContext = createContext<ForkDialogContextValue | null>(null);

export const ForkDialogContextProvider = ForkDialogContext.Provider;

/**
 * Hook for descendants of AppShell. Returns `null` outside the provider
 * (e.g. in isolated component tests), so callers should no-op or hide
 * the affordance when `null`.
 */
export function useForkDialog(): ForkDialogContextValue | null {
  return useContext(ForkDialogContext);
}
