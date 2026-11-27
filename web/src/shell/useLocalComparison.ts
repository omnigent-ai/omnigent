// The source Changes review reads: a session's changed files under its effective
// base, and the reference design's view of that comparison. It reuses the
// session's shared comparison, so the badge, Changes list and both reviews agree.

import { useMemo } from "react";

import { useChoosingComparisonOwner } from "@/hooks/useComparisonOwner";
import {
  type WorkingCopyChanges,
  useWorkingCopyBaseCommands,
  useWorkingCopyChanges,
  workingCopyComparisonActions,
} from "@/hooks/useWorkingCopyChanges";
import { type ComparisonChoice, comparisonChoiceOf } from "@/lib/comparisonChoice";
import { localComparisonView, type LocalComparisonView } from "@/lib/localComparison";
import { useBaseIntent } from "@/lib/workingCopyBasePreference";
import type { ComparisonAvailability } from "./useVisitComparison";
import { useWorkingCopyBase } from "./workingCopyBase";

export interface LocalComparisonSource {
  /** The changed files, generation and gates the review's reads are checked against. */
  changes: WorkingCopyChanges;
  view: LocalComparisonView;
  /** What the session compares with; settled once its workspace, and so its typed comparison, is known. */
  choice: {
    shown: ComparisonChoice;
    settled: boolean;
    /** Whether `choice` can be applied: the menu's own checks, and typed revisions' support. */
    availability: (choice: ComparisonChoice) => ComparisonAvailability;
    choose: (choice: ComparisonChoice) => void;
  };
}

/** A session's changes and their comparison. Only a review on screen (`active`) keeps it current. */
export function useLocalComparison(
  sessionId: string,
  { active }: { active: boolean },
): LocalComparisonSource {
  const changes = useWorkingCopyChanges(sessionId, { kind: active ? "surface" : "file" });
  const headBase = useWorkingCopyBase(sessionId);
  const commands = useWorkingCopyBaseCommands(sessionId, { headIsKnown: headBase !== null });
  const choosingOwner = useChoosingComparisonOwner();
  const intent = useBaseIntent(choosingOwner.repository, choosingOwner.workspaceKey);
  const { comparison, revisionSupport, custom, permitted, readable, refetch } = changes;
  const view = useMemo(() => {
    const recoveries = workingCopyComparisonActions(sessionId, choosingOwner);
    return localComparisonView({
      comparison,
      headIsKnown: headBase !== null,
      revisionSupport,
      custom,
      intent,
      commands,
      permitted,
      readable,
      actions: {
        refetchHead: () => void refetch(),
        retryBranchPoint: recoveries.retry,
        showLastCommit: recoveries.showLastCommit,
        compareAgain: recoveries.compareAgain,
        compareRevisions: recoveries.compareRevisions,
      },
    });
  }, [
    sessionId,
    choosingOwner,
    comparison,
    headBase,
    revisionSupport,
    custom,
    intent,
    commands,
    permitted,
    readable,
    refetch,
  ]);
  const settled = changes.workspace !== null;
  const choice = useMemo(() => {
    const recoveries = workingCopyComparisonActions(sessionId, choosingOwner);
    const commandFor = (next: ComparisonChoice & { kind: "saved" }) =>
      commands.find((command) => command.id === `compare-${next.intent}`);
    return {
      shown: comparisonChoiceOf(intent, custom),
      settled,
      availability: (next: ComparisonChoice): ComparisonAvailability => {
        if (next.kind === "saved") {
          const reason = commandFor(next)?.unavailableReason;
          return reason ? { unavailable: reason } : "now";
        }
        if (revisionSupport.kind === "available") return "now";
        if (revisionSupport.kind === "checking") return "later";
        return { unavailable: revisionSupport.message };
      },
      // Runs the same command as the menu, so the choice is saved as a person's would be.
      choose: (next: ComparisonChoice) => {
        if (next.kind === "typed")
          recoveries.compareRevisions({ base: next.base, head: next.head });
        else commandFor(next)?.run();
      },
    };
  }, [sessionId, choosingOwner, intent, custom, settled, commands, revisionSupport]);
  return { changes, view, choice };
}
