// Which comparison owner the views below read a session's changes from: the
// document's shared one, unless a review shows something of its own, such as a
// commit, without changing what the session compares everywhere else.

import { createContext, useContext } from "react";

import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import { workingCopyComparisons } from "@/lib/workingCopyComparisons";

export const ComparisonOwnerContext = createContext<WorkingCopyComparisonOwner | null>(null);

/** The owner views here register with and read from. */
export function useComparisonOwner(): WorkingCopyComparisonOwner {
  return useContext(ComparisonOwnerContext) ?? workingCopyComparisons();
}
