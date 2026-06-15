// TanStack Query wrapper around the resources filesystem file-content endpoint:
// `GET /v1/sessions/{sessionId}/resources/environments/{environmentId}/filesystem/{path}`.
//
// Text files have `encoding: "utf-8"` and return content inline.
// Binary files that cannot be decoded as UTF-8 have `encoding: "base64"`.
// The query is disabled when either `conversationId` or `path` is null/undefined.

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { authenticatedFetch } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";

// The primary workspace environment is always "default".  This hook targets
// the primary workspace; pass a different id if terminal environments are needed.
const DEFAULT_ENVIRONMENT_ID = "default";

export interface FileContentResponse {
  object: "session.environment.filesystem.file_content";
  path: string;
  content_type: string | null;
  encoding: "utf-8" | "base64";
  content: string;
  bytes: number;
  truncated?: boolean;
}

export async function fetchFileContent(
  conversationId: string,
  path: string,
): Promise<FileContentResponse> {
  // Encode each path segment individually so slashes remain structural.
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const url =
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${encodedPath}`;
  const res = await authenticatedFetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as FileContentResponse;
}

/**
 * Convert a ``FileContentResponse`` into a ``Blob`` with the correct MIME type.
 *
 * Handles both UTF-8 text files and base64-encoded binary files. This is the
 * single source of truth for MIME/encoding handling — callers that already
 * have a response object (e.g. FileViewer) call this directly instead of
 * re-fetching.
 *
 * :param data: The file content response from the filesystem API.
 * :returns: A ``Blob`` suitable for a browser download.
 */
export function fileContentToBlob(data: FileContentResponse): Blob {
  if (data.encoding === "base64") {
    const binary = atob(data.content);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: data.content_type ?? "application/octet-stream" });
  }
  return new Blob([data.content], { type: data.content_type ?? "text/plain" });
}

/**
 * Programmatically trigger a browser file download for the given ``Blob``.
 *
 * Creates a temporary object URL, clicks a synthetic ``<a>`` element to
 * initiate the download, then immediately cleans up both the element and
 * the URL.
 *
 * :param blob: The file data to download.
 * :param filename: The suggested filename presented to the browser's save dialog.
 */
export function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * Fetch a workspace file and trigger a browser download of its contents.
 *
 * Logs a console warning when the server indicates the file was truncated
 * (``truncated: true``) so callers know the downloaded content may be
 * incomplete.
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative file path, e.g. ``"src/main.py"``.
 */
export async function downloadWorkspaceFile(conversationId: string, path: string): Promise<void> {
  const data = await fetchFileContent(conversationId, path);
  if (data.truncated) {
    console.warn(
      `[ap-web] File "${path}" was truncated by the server — downloaded content may be incomplete.`,
    );
  }
  triggerBrowserDownload(fileContentToBlob(data), path.split("/").pop() ?? path);
}

/**
 * Fetch the content of a workspace file for the given conversation.
 *
 * Disabled (no request made) when `conversationId` or `path` is
 * null / undefined.
 *
 * Fires one trailing invalidation when the session transitions from active
 * (running/waiting) to idle so the viewer picks up any file writes the agent
 * made during the turn. No polling is used — the invalidation fires exactly
 * once at end-of-turn, avoiding continuous refetches that would reset the
 * editor's scroll and cursor position.
 */
export function useFileContent(conversationId: string | undefined, path: string | null) {
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const sessionActive =
    !!conversationId &&
    conversationId === focusedId &&
    (sessionStatus === "running" || sessionStatus === "waiting");
  const runnerOnline = useSessionRunnerOnline(conversationId);
  const queryClient = useQueryClient();

  const prevRef = useRef<{ id: string | undefined; active: boolean }>({
    id: conversationId,
    active: sessionActive,
  });
  useEffect(() => {
    const sameSession = prevRef.current.id === conversationId;
    const justWentIdle = sameSession && prevRef.current.active && !sessionActive;
    prevRef.current = { id: conversationId, active: sessionActive };
    if (justWentIdle && conversationId && path) {
      void queryClient.invalidateQueries({
        queryKey: ["file-content", conversationId, path],
      });
    }
  }, [conversationId, path, sessionActive, queryClient]);

  return useQuery({
    queryKey: ["file-content", conversationId, path],
    queryFn: () => fetchFileContent(conversationId!, path!),
    enabled: !!conversationId && !!path && runnerOnline !== false,
    staleTime: 5_000,
  });
}
