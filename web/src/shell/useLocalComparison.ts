// The source Changes review reads: a session's changed files under its effective
// base, and the reference design's view of that comparison. It reuses the
// session's shared comparison, so the badge, Changes list and both reviews agree.

import { useMemo } from "react";

import {
  type WorkingCopyChanges,
  useWorkingCopyBaseCommands,
  useWorkingCopyChanges,
  workingCopyComparisonActions,
} from "@/hooks/useWorkingCopyChanges";
import { localComparisonView, type LocalComparisonView } from "@/lib/localComparison";
import { useWorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";
import { useWorkingCopyBase } from "./workingCopyBase";

export interface LocalComparisonSource {
  /** The changed files, generation and gates the review's reads are checked against. */
  changes: WorkingCopyChanges;
  view: LocalComparisonView;
}

/** A session's changes and their comparison. Only a review on screen (`active`) keeps it current. */
export function useLocalComparison(
  sessionId: string,
  { active }: { active: boolean },
): LocalComparisonSource {
  const changes = useWorkingCopyChanges(sessionId, { kind: active ? "surface" : "file" });
  const headBase = useWorkingCopyBase(sessionId);
  const commands = useWorkingCopyBaseCommands(sessionId, { headIsKnown: headBase !== null });
  const intent = useWorkingCopyBaseIntent();
  const { comparison, revisionSupport, custom, permitted, readable, refetch } = changes;
  const view = useMemo(() => {
    const recoveries = workingCopyComparisonActions(sessionId);
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
  return { changes, view };
}
