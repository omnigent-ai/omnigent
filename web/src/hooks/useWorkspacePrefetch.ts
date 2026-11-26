// Loads what the Files tree is likely to show next, so opening it is instant.
// Pausing on a folder or file (pointer or keyboard focus) loads its listing or
// contents. Fresh cache entries aren't fetched again, large or video files are
// never prefetched, and nothing loads while the session's workspace can't be read.

import { useCallback, useEffect, useMemo, useRef } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { FILE_CONTENT_POLL_MAX_BYTES, fileContentQuery } from "@/hooks/useFileContent";
import {
  joinBrowseLocation,
  useWorkspaceServeable,
  workspaceDirectoryQuery,
} from "@/hooks/useWorkspaceChangedFiles";
import { isVideoFile } from "@/lib/video";

/** How long the pointer or focus rests on a row before its target loads. */
export const PREFETCH_DWELL_MS = 100;
/** Files larger than the viewer polls aren't loaded ahead. */
export const FILE_PREFETCH_MAX_BYTES = FILE_CONTENT_POLL_MAX_BYTES;

/** A row the user is about to open: a folder, or a file with its known size. */
export type PrefetchTarget =
  { kind: "directory"; path: string } | { kind: "file"; path: string; bytes: number | null };

export interface WorkspacePrefetch {
  /** The pointer or focus reached a row; its target loads after a short pause. */
  intend: (target: PrefetchTarget) => void;
  /** The pointer or focus left before the pause ended. */
  cancel: () => void;
}

interface PrefetchScope {
  conversationId: string;
  /** The browsed location tree paths are relative to; "" for the workspace root. */
  location: string;
  refreshToken: number;
  /** The tree leaves out what Git ignores, so its folders list without it. */
  hideIgnored?: boolean;
}

/** Whether a file is small enough, and of a kind the viewer reads, to load ahead. */
export function prefetchableFile(path: string, bytes: number | null): boolean {
  return bytes !== null && bytes <= FILE_PREFETCH_MAX_BYTES && !isVideoFile(path);
}

/** Load one target into the cache, unless a fresh answer is already there. */
export function prefetchTarget(
  queryClient: QueryClient,
  scope: PrefetchScope,
  target: PrefetchTarget,
): Promise<void> {
  const { conversationId, location, refreshToken, hideIgnored = false } = scope;
  if (target.kind === "directory") {
    return queryClient.prefetchQuery(
      workspaceDirectoryQuery(conversationId, target.path, location, refreshToken, hideIgnored),
    );
  }
  if (!prefetchableFile(target.path, target.bytes)) return Promise.resolve();
  return queryClient.prefetchQuery(
    fileContentQuery(conversationId, joinBrowseLocation(location, target.path)),
  );
}

/**
 * Prefetch for one session's Files tree. Off (every call a no-op) without a
 * session or while its workspace can't be read; becoming unreadable also
 * cancels a pause under way.
 */
export function useWorkspacePrefetch({
  conversationId,
  location,
  refreshToken,
  hideIgnored = false,
}: {
  conversationId: string | undefined;
  location: string;
  refreshToken: number;
  hideIgnored?: boolean;
}): WorkspacePrefetch {
  const queryClient = useQueryClient();
  // The same gate the tree's own queries use: nothing loads once the runner
  // and host are both known to be unavailable.
  const enabled = useWorkspaceServeable(conversationId) !== false;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scope = useMemo<PrefetchScope | null>(
    () =>
      enabled && conversationId ? { conversationId, location, refreshToken, hideIgnored } : null,
    [enabled, conversationId, location, refreshToken, hideIgnored],
  );

  const cancel = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  // A changed session, location, or readability ends any pause under way.
  useEffect(() => cancel, [cancel, scope]);

  const intend = useCallback(
    (target: PrefetchTarget) => {
      cancel();
      if (!scope) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        void prefetchTarget(queryClient, scope, target);
      }, PREFETCH_DWELL_MS);
    },
    [cancel, queryClient, scope],
  );

  return useMemo(() => ({ intend, cancel }), [intend, cancel]);
}
