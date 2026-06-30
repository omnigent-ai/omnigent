// TanStack Query hook for the filesystem file-diff endpoint:
// `GET /v1/sessions/{sessionId}/resources/environments/{environmentId}/diff/{path}`.
//
// Returns `before` (content at first observed modification) and `after`
// (current content) as strings — either may be null:
//   before=null → new file (no pre-session snapshot available)
//   after=null  → deleted file
//
// The query is disabled unless the file appears in the changed-files list
// (only files that were created, modified, or deleted this session have diff
// data) and the runner is online.

import { useQuery } from "@tanstack/react-query";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { authenticatedFetch } from "@/lib/identity";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";

// The primary workspace environment is always "default".
const DEFAULT_ENVIRONMENT_ID = "default";

export interface FileDiffResponse {
  object: "session.environment.filesystem.file_diff";
  path: string;
  /** File content at the time of the first modification, or null for new files. */
  before: string | null;
  /** Current file content, or null for deleted files. */
  after: string | null;
}

async function fetchFileDiff(conversationId: string, path: string): Promise<FileDiffResponse> {
  // Encode each path segment individually so slashes remain structural.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const url =
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/diff/${encodedPath}`;
  const res = await authenticatedFetch(url);
  if (!res.ok) {
    // Surface the server's reason (e.g. "git status timed out after 5.0s")
    // so the diff view shows what actually went wrong rather than a bare
    // status code — mirroring the changed-files panel.
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      if (body?.error?.message) message = body.error.message;
    } catch {
      // Non-JSON body (gateway/front-door error) — keep the status line.
    }
    throw new Error(message);
  }
  return (await res.json()) as FileDiffResponse;
}

/**
 * Fetch before/after diff content for a changed workspace file.
 *
 * Disabled (no request made) when:
 * - `conversationId` or `path` is null / undefined
 * - the runner is offline
 * - the file does not appear in the session's changed-files list
 */
export function useFileDiff(conversationId: string | undefined, path: string | null) {
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const changedFiles = useWorkspaceChangedFiles(conversationId);
  const isInChangedFiles = changedFiles.data?.data.some((f) => f.path === path) ?? false;

  return useQuery({
    queryKey: ["file-diff", conversationId, path],
    queryFn: () => fetchFileDiff(conversationId!, path!),
    enabled: !!conversationId && !!path && runnerOnline !== false && isInChangedFiles,
    staleTime: 5_000,
  });
}
