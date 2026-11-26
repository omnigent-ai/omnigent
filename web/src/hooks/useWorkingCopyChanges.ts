// A session's changed files under the base it compares with: the last commit
// from the runner, or the branch point from the session's host. Every view of a
// session's changes reads this, so a file committed on the branch shows in the
// badge, the Changes list, the file viewer, and the review together, or nowhere.

import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
  useWorkspaceServeable,
  type WorkspaceChangedFilesResult,
} from "@/hooks/useWorkspaceChangedFiles";
import { useComparisonOwner } from "@/hooks/useComparisonOwner";
import { useSession } from "@/hooks/useSession";
import type { ComparisonWorkspace, RevisionSpec } from "@/lib/customComparison";
import { canBrowseWorkspaceFiles } from "@/lib/permissionsApi";
import { getSessionHost, subscribeSessionHostChanges } from "@/lib/sessionHost";
import {
  type BranchPointSupport,
  effectiveComparison,
  type EffectiveComparison,
  RECONNECTING,
} from "@/lib/workingCopyComparison";
import type {
  ComparisonConsumerKind,
  ComparisonOwnerSnapshot,
  WorkingCopyComparisonOwner,
} from "@/lib/workingCopyComparisonOwner";
import { workingCopyComparisons } from "@/lib/workingCopyComparisons";
import {
  type WorkingCopyBaseCommand,
  workingCopyBaseCommands,
} from "@/lib/workingCopyBaseCommands";
import { useWorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";

function useSessionHostId(sessionId: string | undefined): string | null {
  return useSyncExternalStore(subscribeSessionHostChanges, () =>
    sessionId ? getSessionHost(sessionId) : null,
  );
}

const NO_SESSION = "";

/** What a view of a session's changes reads, in the last-commit list's shape. */
export interface WorkingCopyChanges {
  /** The list under the effective base; undefined while it's pending, unavailable, or loading. */
  data: WorkspaceChangedFilesResult | undefined;
  isError: boolean;
  error: Error | null;
  isSuccess: boolean;
  /** Waiting for the first list under the effective base. */
  isLoading: boolean;
  dataUpdatedAt: number;
  comparison: EffectiveComparison;
  /** Whether the branch point can be compared here at all. */
  support: BranchPointSupport;
  /** Whether a typed base and head can be compared here. */
  revisionSupport: BranchPointSupport;
  /** The base and head the person typed for this session, if any. */
  custom: RevisionSpec | null;
  /** The session's workspace, which typed comparisons and their suggestions belong to. */
  workspace: ComparisonWorkspace | null;
  /** Lists again under both bases; resolves when the last-commit list is back. */
  refetch: () => Promise<unknown>;
  /** The owner generation the comparison belongs to, for checking a read is still current. */
  generation: number;
  /** Whether the viewer may read this session's files at all. */
  permitted: boolean;
  /** Changes when the app verified the session's access again; refused reads retry then. */
  accessEpoch: number;
  /** Changes each time the person chooses a comparison for this session here. */
  choices: number;
  /** Whether new reads may start now: permitted, and the workspace can be reached. */
  readable: boolean;
}

/**
 * A session's changed files under its effective base. `kind` says how the view
 * uses them: only a visible Changes list or review ("surface") polls.
 */
export function useWorkingCopyChanges(
  sessionId: string | undefined,
  { enabled = true, kind }: { enabled?: boolean; kind: ComparisonConsumerKind },
): WorkingCopyChanges {
  const owner = useComparisonOwner();
  const intent = useWorkingCopyBaseIntent();
  // Every view checks the session's own file permission, so losing a grant stops them all.
  const { session } = useSession(sessionId);
  const permitted = canBrowseWorkspaceFiles(session);
  const allowed = enabled && permitted;
  const head = useWorkspaceChangedFiles(sessionId, { enabled: allowed });
  const serveable = useWorkspaceServeable(sessionId);
  const environment = useWorkspaceEnvironment(sessionId, { enabled: allowed });
  const sessionHostId = useSessionHostId(sessionId);
  const readable =
    allowed && !!sessionId && serveable !== false && environment.data?.available === true;

  const root = environment.data?.root ?? null;
  const consumer = useMemo(
    () => ({ kind, enabled: readable, sessionHostId, root }),
    [kind, readable, sessionHostId, root],
  );
  // Registered once per session and kind; gate changes update the registration.
  const latestConsumer = useRef(consumer);
  latestConsumer.current = consumer;
  const registration = useRef<{ sessionId: string; token: symbol } | null>(null);
  useEffect(() => {
    if (!sessionId) return;
    const token = owner.register(sessionId, latestConsumer.current);
    registration.current = { sessionId, token };
    return () => {
      owner.unregister(sessionId, token);
      registration.current = null;
    };
  }, [owner, sessionId, kind]);
  useEffect(() => {
    const current = registration.current;
    if (current) owner.update(current.sessionId, current.token, consumer);
  }, [owner, consumer]);

  const snapshot = useOwnerSnapshot(owner, sessionId);
  const multipleRepositories = useMemo(
    () => head.data?.data.some((file) => !!file.repository_root) ?? false,
    [head.data],
  );
  const comparison = useMemo((): EffectiveComparison => {
    const effective = effectiveComparison({
      intent,
      support: snapshot.support,
      multipleRepositories,
      established: snapshot.established,
      problem: snapshot.problem,
      connectTimedOut: snapshot.connectTimedOut,
      headOverride: snapshot.headOverride,
      custom: snapshot.custom,
      revisionSupport: snapshot.revisionSupport,
    });
    // The workspace can't be reached for now: the comparison stays on screen until it's back.
    if (effective.base === "branch-point" && !effective.status && allowed && !readable) {
      return { ...effective, status: { kind: "reconnecting", message: RECONNECTING } };
    }
    return effective;
  }, [intent, snapshot, multipleRepositories, allowed, readable]);
  const branchPointData = useMemo<WorkspaceChangedFilesResult | undefined>(
    () =>
      comparison.base === "branch-point"
        ? { available: true, data: comparison.comparison.entries }
        : undefined,
    [comparison],
  );

  const { refetch: refetchHead } = head;
  const refetch = useCallback(() => {
    if (sessionId) owner.invalidate(sessionId);
    return refetchHead();
  }, [owner, sessionId, refetchHead]);
  const shared = {
    comparison,
    support: snapshot.support,
    revisionSupport: snapshot.revisionSupport,
    custom: snapshot.custom,
    workspace: snapshot.workspace,
    generation: snapshot.generation,
    refetch,
    permitted,
    accessEpoch: snapshot.accessEpoch,
    choices: snapshot.choices,
    readable,
  };

  if (!allowed) {
    return {
      ...shared,
      data: undefined,
      isError: false,
      error: null,
      isSuccess: false,
      isLoading: false,
      dataUpdatedAt: 0,
    };
  }
  if (comparison.base === "head") {
    return {
      ...shared,
      data: head.data,
      isError: head.isError,
      error: head.error,
      isSuccess: head.isSuccess,
      isLoading: head.isLoading,
      dataUpdatedAt: head.dataUpdatedAt,
    };
  }
  if (comparison.base === "branch-point") {
    return {
      ...shared,
      // Kept through a brief loss of the runner or host; only permission takes it away.
      data: branchPointData,
      isError: false,
      error: null,
      isSuccess: true,
      isLoading: false,
      dataUpdatedAt: comparison.comparison.listedAt,
    };
  }
  return {
    ...shared,
    data: undefined,
    isError: false,
    error: null,
    isSuccess: false,
    isLoading: comparison.base === "pending",
    dataUpdatedAt: 0,
  };
}

function useOwnerSnapshot(
  owner: WorkingCopyComparisonOwner,
  sessionId: string | undefined,
): ComparisonOwnerSnapshot {
  const key = sessionId ?? NO_SESSION;
  return useSyncExternalStore(
    (listener) => owner.subscribe(key, listener),
    () => owner.snapshot(key),
  );
}

/**
 * The commands that choose a session's base and refresh its changes; empty without a session.
 * `headIsKnown` comes from a view that probed the workspace for Git.
 */
export function useWorkingCopyBaseCommands(
  sessionId: string | undefined,
  { headIsKnown = true }: { headIsKnown?: boolean } = {},
): WorkingCopyBaseCommand[] {
  const intent = useWorkingCopyBaseIntent();
  // Base commands change the session's own comparison, whichever owner the view reads.
  const { support, revisionSupport, custom } = useOwnerSnapshot(
    workingCopyComparisons(),
    sessionId,
  );
  const queryClient = useQueryClient();
  return useMemo(
    () =>
      sessionId
        ? workingCopyBaseCommands({
            sessionId,
            intent,
            custom,
            support,
            revisionSupport,
            headIsKnown,
            owner: workingCopyComparisons(),
            queryClient,
          })
        : [],
    [sessionId, intent, custom, support, revisionSupport, headIsKnown, queryClient],
  );
}

/** A session's comparison recoveries, for its status row and placeholder. */
export function workingCopyComparisonActions(sessionId: string) {
  const owner = workingCopyComparisons();
  return {
    retry: () => owner.retry(sessionId),
    showLastCommit: () => owner.showLastCommit(sessionId),
    /** Ends "Show uncommitted changes" and compares with the saved base again. */
    compareAgain: () => owner.chose(sessionId),
    /** Compares the typed base and head for this session. */
    compareRevisions: (spec: RevisionSpec) => owner.compareRevisions(sessionId, spec),
  };
}

const DIFF_TEXT_KEYS = ["file-diff"] as const;

/**
 * Drops a session's cached diff text, and stops reads of it, once its file
 * permission is gone. Mounted by the views that cache that text.
 */
export function useForgetDiffTextWhenRevoked(sessionId: string | undefined, permitted: boolean) {
  useForgetWhenRevoked(sessionId, permitted, DIFF_TEXT_KEYS);
}

/** Drops a session's queries under each of `keys`, and stops their reads, once its permission is gone. */
export function useForgetWhenRevoked(
  sessionId: string | undefined,
  permitted: boolean,
  keys: readonly string[],
) {
  const queryClient = useQueryClient();
  const wasPermitted = useRef(permitted);
  const keyList = keys.join("\n");
  useEffect(() => {
    const revoked = wasPermitted.current && !permitted;
    wasPermitted.current = permitted;
    if (!revoked || !sessionId) return;
    for (const key of keyList.split("\n")) {
      const filters = { queryKey: [key, sessionId] };
      void queryClient.cancelQueries(filters);
      queryClient.removeQueries(filters);
    }
  }, [queryClient, sessionId, permitted, keyList]);
}
