// The commands that choose what a session's changes are compared with, and
// refresh them. One definition serves the review's toolbar menu, the Changes
// tab's menu, the Changes review's quick choices, and ⌘K, so their labels,
// checks, and availability can't drift.

import type { QueryClient } from "@tanstack/react-query";

import { type RevisionSpec, sameRevisions } from "@/lib/customComparison";
import type { BranchPointSupport } from "@/lib/workingCopyComparison";
import {
  type WorkingCopyBaseIntent,
  writeWorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";
import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import { invalidateWorkingCopyChanges } from "@/lib/workingCopyComparisons";

export type WorkingCopyBaseCommandId =
  | "compare-automatic"
  | "compare-head"
  | "compare-branch-point"
  | "compare-last-commit"
  | "refresh-changes";

/** The comparisons a person can pick in one step, in the order they're offered. */
export type QuickComparisonId = "compare-head" | "compare-branch-point" | "compare-last-commit";
export const QUICK_COMPARISONS: readonly QuickComparisonId[] = [
  "compare-head",
  "compare-branch-point",
  "compare-last-commit",
];

export const UNKNOWN_BASELINE_TITLE = "The host didn't say what these changes are compared with.";
export const CHECKING_REVISIONS = "Checking whether this session can compare other revisions…";

/** What the last commit changed: its parent compared with it. */
export const LAST_COMMIT_REVISIONS: RevisionSpec = { base: "HEAD~1", head: "HEAD" };

export interface WorkingCopyBaseCommand {
  id: WorkingCopyBaseCommandId;
  /** The menu's label, and the value's name on ⌘K's "Compare Changes with" page. */
  label: string;
  /** The same name in sentence case, for prose and the Changes review's quick choices. */
  name: string;
  /** For a base choice, whether it's the saved one; absent for Refresh. */
  checked?: boolean;
  /** Why it can't run here; null when it can. */
  unavailableReason: string | null;
  run: () => void;
}

const BASE_CHOICES: readonly {
  id: WorkingCopyBaseCommandId;
  intent: WorkingCopyBaseIntent;
  label: string;
  name: string;
}[] = [
  { id: "compare-automatic", intent: "automatic", label: "Automatic", name: "Automatic" },
  {
    id: "compare-head",
    intent: "head",
    label: "Uncommitted Changes",
    name: "Uncommitted changes",
  },
  {
    id: "compare-branch-point",
    intent: "branch-point",
    label: "All Branch Changes",
    name: "All branch changes",
  },
];

/** Why a typed base and head can't be compared here; null when they can. */
export function revisionsUnavailableReason(support: BranchPointSupport): string | null {
  if (support.kind === "available") return null;
  return support.kind === "checking" ? CHECKING_REVISIONS : support.message;
}

/**
 * The base commands for one session. Choosing a base, even the current one,
 * ends that session's "Show uncommitted changes" and typed comparison; Last
 * Commit is a typed comparison of its own. Refresh asks the desktop app for a
 * new comparison and the runner for a new list.
 */
export function workingCopyBaseCommands({
  sessionId,
  intent,
  custom = null,
  support,
  revisionSupport,
  headIsKnown = true,
  owner,
  queryClient,
  writeIntent = writeWorkingCopyBaseIntent,
}: {
  sessionId: string;
  intent: WorkingCopyBaseIntent;
  /** The typed base and head the session shows, if any; no saved base is checked then. */
  custom?: RevisionSpec | null;
  support: BranchPointSupport;
  /** Whether a typed base and head can be compared for this session. */
  revisionSupport: BranchPointSupport;
  /**
   * The workspace is known to be Git, so the runner's list is against HEAD.
   * Where nothing probes the workspace, the runner's list is taken to be.
   */
  headIsKnown?: boolean;
  owner: WorkingCopyComparisonOwner;
  queryClient: QueryClient;
  /** Saves a chosen base: the device's preference, or a repository folder's own. */
  writeIntent?: (intent: WorkingCopyBaseIntent) => void;
}): WorkingCopyBaseCommand[] {
  const choices = BASE_CHOICES.map(
    ({ id, intent: choice, label, name }): WorkingCopyBaseCommand => ({
      id,
      label,
      name,
      checked: custom === null && intent === choice,
      unavailableReason:
        choice === "branch-point" && support.kind === "unsupported"
          ? support.message
          : choice === "head" && !headIsKnown
            ? UNKNOWN_BASELINE_TITLE
            : null,
      run: () => {
        writeIntent(choice);
        owner.chose(sessionId);
      },
    }),
  );
  return [
    ...choices,
    {
      id: "compare-last-commit",
      label: "Last Commit",
      name: "Last commit",
      checked: sameRevisions(custom, LAST_COMMIT_REVISIONS),
      unavailableReason: revisionsUnavailableReason(revisionSupport),
      run: () => owner.compareRevisions(sessionId, LAST_COMMIT_REVISIONS),
    },
    {
      id: "refresh-changes",
      label: "Refresh Changes",
      name: "Refresh changes",
      unavailableReason: null,
      run: () => {
        owner.refresh(sessionId);
        invalidateWorkingCopyChanges(queryClient, sessionId);
      },
    },
  ];
}
