// Whether this pull request can take attachments where it is edited, for every comment form on
// the page; the description gets the same facts as props.

import { createContext, type ReactNode, useContext, useMemo } from "react";

export interface PullRequestAttachmentsAvailability {
  /** Uploads work here. */
  available: boolean;
  /** Saved on this Mac, but uploads still need the session's machine. */
  needsHost: boolean;
}

const UNAVAILABLE: PullRequestAttachmentsAvailability = { available: false, needsHost: false };

export const PullRequestAttachmentsContext =
  createContext<PullRequestAttachmentsAvailability>(UNAVAILABLE);

export function usePullRequestAttachmentsAvailability(): PullRequestAttachmentsAvailability {
  return useContext(PullRequestAttachmentsContext);
}

/** Shares whether attachments work here with every comment form inside. */
export function PullRequestAttachmentsProvider({
  available,
  needsHost,
  children,
}: PullRequestAttachmentsAvailability & { children: ReactNode }) {
  const value = useMemo(() => ({ available, needsHost }), [available, needsHost]);
  return (
    <PullRequestAttachmentsContext.Provider value={value}>
      {children}
    </PullRequestAttachmentsContext.Provider>
  );
}
