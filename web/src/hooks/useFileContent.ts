// TanStack Query wrapper around the resources filesystem file-content endpoint:
// `GET /v1/sessions/{sessionId}/resources/environments/{environmentId}/filesystem/{path}`.
//
// Text files have `encoding: "utf-8"` and return content inline.
// Binary files that cannot be decoded as UTF-8 have `encoding: "base64"`.
// The query is disabled when either `conversationId` or `path` is null/undefined.

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { withBasePath } from "@/lib/basePath";
import { isDatabricksWorkspace } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  browseLocationBase,
  browseLocationSegment,
  useWorkspaceServeable,
} from "@/hooks/useWorkspaceChangedFiles";
import { fileWritesSettled } from "@/hooks/useWriteFileContent";
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

/**
 * Build the filesystem URL for a workspace file.
 *
 * Reuses the shared browse-location wire form: a per-segment-encoded path with
 * literal slashes and, for an absolute path (a file opened while browsing
 * outside the workspace), the base named by `?base=host`. Keeping this on the
 * shared helpers means the slash-merge-safe contract lives in one place (see
 * `browseLocationSegment`).
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative or host-absolute file path.
 * :param query: Extra query parameters, e.g. ``{ download: "true" }``.
 */
function workspaceFileUrl(
  conversationId: string,
  path: string,
  query: Record<string, string> = {},
): string {
  const base = browseLocationBase(path);
  const params = new URLSearchParams(base ? { ...query, base } : query).toString();
  return (
    `/v1/sessions/${encodeURIComponent(conversationId)}` +
    `/resources/environments/${DEFAULT_ENVIRONMENT_ID}/filesystem/${browseLocationSegment(path)}` +
    (params ? `?${params}` : "")
  );
}

/** Read complete media bytes through the host's authenticated transport, with cancellation. */
export async function fetchWorkspaceFileBlob(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await authenticatedFetch(
    workspaceFileUrl(conversationId, path, { download: "true" }),
    {
      signal,
    },
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.blob();
}

export async function fetchFileContent(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse> {
  const res = await authenticatedFetch(workspaceFileUrl(conversationId, path), { signal });
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
  clickDownloadLink(url, filename);
  URL.revokeObjectURL(url);
}

function clickDownloadLink(href: string, filename: string): void {
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
}

/**
 * Download a workspace file's complete bytes.
 *
 * Asks the filesystem endpoint for the raw file (`download=true`), which the
 * server streams with no size cap, rather than the viewer's JSON envelope,
 * which is truncated past the server's read cap.
 *
 * In a browser this is a plain same-origin link click: the browser streams the
 * attachment straight to disk and shows it in its download UI at once, and the
 * session cookie or proxy identity header rides along as on any request. The
 * managed embed owns the transport (auth headers, replica routing) and the
 * mobile shells save http(s) downloads outside the WebView's session, so those
 * fetch the bytes through `authenticatedFetch` and hand them over as a Blob,
 * which only surfaces once the whole file has arrived.
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative file path, e.g. ``"src/main.py"``.
 */
export async function downloadWorkspaceFile(conversationId: string, path: string): Promise<void> {
  const url = workspaceFileUrl(conversationId, path, { download: "true" });
  const filename = path.split("/").pop() ?? path;
  if (!isDatabricksWorkspace() && !isIOSShell() && !isAndroidShell()) {
    // A direct anchor navigation, so it must carry the deployment subpath the
    // fetch paths get for free via hostFetch/authenticatedFetch. Without it a
    // stripping proxy (`/proxy/<port>/`) sends it to the origin root and misses
    // the app. The fetch branch below keeps `url` raw: authenticatedFetch
    // prefixes internally, so passing it prefixed would double the base path.
    clickDownloadLink(withBasePath(url), filename);
    return;
  }
  const res = await authenticatedFetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  triggerBrowserDownload(await res.blob(), filename);
}

/** How often an on-screen text file is re-read to show other writers' edits. */
export const FILE_CONTENT_POLL_MS = 2_000;

/** Larger files refresh only when shown again: every poll downloads the whole file. */
export const FILE_CONTENT_POLL_MAX_BYTES = 256 * 1024;

/** While refreshes keep failing (e.g. the file was deleted), re-check this often. */
export const FILE_CONTENT_FAILING_POLL_MS = 30_000;

/**
 * Decide how often to re-read an on-screen file.
 *
 * Polls only UTF-8 files up to :data:`FILE_CONTENT_POLL_MAX_BYTES`. Binary
 * previews and large (including truncated) files are skipped.
 *
 * :param data: The last successful response, or undefined before the first load.
 * :param failing: True when the latest refresh failed after its retries.
 * :returns: The interval in milliseconds, or ``false`` to not poll.
 */
export function fileContentPollInterval(
  data: FileContentResponse | undefined,
  failing: boolean,
): number | false {
  if (data?.encoding !== "utf-8" || data.bytes > FILE_CONTENT_POLL_MAX_BYTES) return false;
  return failing ? FILE_CONTENT_FAILING_POLL_MS : FILE_CONTENT_POLL_MS;
}

/**
 * Fetch the content of a workspace file for the given conversation.
 *
 * Disabled (no request made) when `conversationId` or `path` is
 * null / undefined, or when `conversationId` is a client-only temp id.
 *
 * Keeps the viewer current with writes from the agent, other sessions and
 * other processes:
 *   - While `visible`, re-reads at {@link fileContentPollInterval}. Hidden
 *     browser tabs pause.
 *   - Re-reads once when the viewer or its browser tab is shown again, and
 *     when the focused session goes from active (running/waiting) to idle.
 * An unchanged read keeps the cached data, so the editors see no change.
 * Changed content is applied in place by `useMarkdownEditorSync`.
 *
 * :param options.visible: True while the viewer is on screen, e.g. an open
 *     panel in the active viewport.
 */
export function useFileContent(
  conversationId: string | undefined,
  path: string | null,
  options: { retry?: boolean; visible?: boolean } = {},
) {
  const visible = options.visible ?? false;
  // A `temp:*` id (navigate-first new-chat window) has no server session yet,
  // so never request `/v1/sessions/temp:*/resources/*` for it.
  const serverId = isTempConvId(conversationId) ? undefined : conversationId;
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const sessionActive =
    !!serverId &&
    serverId === focusedId &&
    (sessionStatus === "running" || sessionStatus === "waiting");
  // Serveable when the runner is online OR (runner offline but) the host can
  // read the workspace from disk — keeps the viewer live while the agent is
  // asleep. `false` only when neither source can answer.
  const serveable = useWorkspaceServeable(serverId);
  const queryClient = useQueryClient();

  const prevRef = useRef<{ id: string | undefined; active: boolean; visible: boolean }>({
    id: serverId,
    active: sessionActive,
    visible,
  });
  useEffect(() => {
    const sameSession = prevRef.current.id === serverId;
    const justWentIdle = sameSession && prevRef.current.active && !sessionActive;
    // A hidden viewer doesn't poll, so catch up as soon as it is shown again.
    const justShown = visible && !prevRef.current.visible;
    prevRef.current = { id: serverId, active: sessionActive, visible };
    if ((justWentIdle || justShown) && serverId && path) {
      void queryClient.invalidateQueries({
        queryKey: ["file-content", serverId, path],
      });
    }
  }, [serverId, path, sessionActive, visible, queryClient]);

  return useQuery({
    queryKey: ["file-content", serverId, path],
    queryFn: async ({ signal }) => {
      await fileWritesSettled(serverId!, path!);
      // Cancelled while waiting (a newer write started): don't send the read.
      signal.throwIfAborted();
      return fetchFileContent(serverId!, path!, signal);
    },
    enabled: !!serverId && !!path && serveable !== false,
    staleTime: 5_000,
    refetchInterval: visible
      ? (query) => fileContentPollInterval(query.state.data, query.state.status === "error")
      : false,
    // Catch up as soon as the browser tab returns, even within staleTime.
    refetchOnWindowFocus: visible ? "always" : false,
    ...(options.retry === undefined ? {} : { retry: options.retry }),
  });
}
