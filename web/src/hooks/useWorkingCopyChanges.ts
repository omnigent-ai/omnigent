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
import { useChoosingComparisonOwner, useComparisonOwner } from "@/hooks/useComparisonOwner";
import { useSession } from "@/hooks/useSession";
import type { ComparisonWorkspace, RevisionSpec } from "@/lib/customComparison";
import { canBrowseWorkspaceFiles } from "@/lib/permissionsApi";
import { getSessionHost, subscribeSessionHostChanges } from "@/lib/sessionHost";
import {
  type BranchPointSupport,
  effectiveComparison,
  type EffectiveComparison,
  hostListing,
  RECONNECTING,
} from "@/lib/workingCopyComparison";
import {
  type ComparisonConsumerKind,
  type ComparisonOwnerSnapshot,
  LAST_COMMIT_SPEC,
  type WorkingCopyComparisonOwner,
} from "@/lib/workingCopyComparisonOwner";
import { workingCopyComparisons } from "@/lib/workingCopyComparisons";
import {
  type WorkingCopyBaseCommand,
  workingCopyBaseCommands,
} from "@/lib/workingCopyBaseCommands";
import { baseIntentSource, useBaseIntent } from "@/lib/workingCopyBasePreference";

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
  // A repository folder's review keeps its own base; the workspace's is the device's preference.
  const intent = useBaseIntent(owner.repository, owner.workspaceKey);
  // Every view checks the session's own file permission, so losing a grant stops them all.
  const { session } = useSession(sessionId);
  const permitted = canBrowseWorkspaceFiles(session);
  const allowed = enabled && permitted;
  // The runner lists only the workspace; a repository folder's changes come from the host.
  const { repository } = owner;
  const head = useWorkspaceChangedFiles(sessionId, { enabled: allowed && !repository });
  const serveable = useWorkspaceServeable(sessionId);
  const environment = useWorkspaceEnvironment(sessionId, { enabled: allowed });
  const sessionHostId = useSessionHostId(sessionId);
  const readable =
    allowed && !!sessionId && serveable !== false && environment.data?.available === true;

  // Always the workspace's root: a folder's owner resolves `repository` against it.
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
    () => (!repository && head.data?.data.some((file) => !!file.repository_root)) || false,
    [repository, head.data],
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
      lastCommitSpec: repository ? LAST_COMMIT_SPEC : null,
    });
    // The workspace can't be reached for now: the comparison stays on screen until it's back.
    if (effective.base === "branch-point" && !effective.status && allowed && !readable) {
      return { ...effective, status: { kind: "reconnecting", message: RECONNECTING } };
    }
    return effective;
  }, [intent, snapshot, multipleRepositories, allowed, readable, repository]);
  const listing = hostListing(comparison);
  const hostData = useMemo<WorkspaceChangedFilesResult | undefined>(
    () => (listing ? { available: true, data: listing.entries } : undefined),
    [listing],
  );

  const { refetch: refetchHead } = head;
  const refetch = useCallback(() => {
    if (sessionId) owner.invalidate(sessionId);
    // A repository folder's changes are all the host's, so the runner isn't asked.
    return repository ? Promise.resolve() : refetchHead();
  }, [owner, sessionId, refetchHead, repository]);
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
  if (listing) {
    return {
      ...shared,
      // Kept through a brief loss of the runner or host; only permission takes it away.
      data: hostData,
      isError: false,
      error: null,
      isSuccess: true,
      isLoading: false,
      dataUpdatedAt: listing.listedAt,
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
  {
    headIsKnown = true,
    owner: givenOwner,
  }: { headIsKnown?: boolean; owner?: WorkingCopyComparisonOwner } = {},
): WorkingCopyBaseCommand[] {
  // Base commands change the session's own comparison, or a repository folder's review's.
  const choosing = useChoosingComparisonOwner();
  const owner = givenOwner ?? choosing;
  const intent = useBaseIntent(owner.repository, owner.workspaceKey);
  const { support, revisionSupport, custom } = useOwnerSnapshot(owner, sessionId);
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
            owner,
            queryClient,
            writeIntent: baseIntentSource(owner.repository, owner.workspaceKey).write,
          })
        : [],
    [sessionId, intent, custom, support, revisionSupport, headIsKnown, owner, queryClient],
  );
}

/** A session's comparison recoveries, for its status row and placeholder, through `owner`. */
export function workingCopyComparisonActions(
  sessionId: string,
  owner: WorkingCopyComparisonOwner = workingCopyComparisons(),
) {
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
