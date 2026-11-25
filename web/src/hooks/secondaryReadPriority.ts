// Where a pull request view's secondary reads wait among the shell's GitHub reads: its merge
// options, commits, pending review and GitHub's own stack. While Changes shows, the user waits on
// the patch, so they read ahead of time instead, and the shell keeps a slot free for the patch.

import { createContext, useContext } from "react";

import type { CodeHostReadPriority } from "@/lib/nativeBridge";

const SecondaryReadPriorityContext = createContext<CodeHostReadPriority>("interactive");

/** Names the priority the secondary reads below it want; a read the user asked for elsewhere. */
export const SecondaryReadPriorityProvider = SecondaryReadPriorityContext.Provider;

/** The priority this view's secondary reads want now. */
export function useSecondaryReadPriority(): CodeHostReadPriority {
  return useContext(SecondaryReadPriorityContext);
}

/** Read ahead while Changes shows and the patch is what the user waits on; asked for otherwise. */
export function secondaryReadPriority(changesShown: boolean): CodeHostReadPriority {
  return changesShown ? "prefetch" : "interactive";
}
