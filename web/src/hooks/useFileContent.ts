// TanStack Query wrapper around the resources filesystem file-content endpoint:
// `GET /v1/sessions/{sessionId}/resources/environments/{environmentId}/filesystem/{path}`.
//
// Text files have `encoding: "utf-8"` and return content inline.
// Binary files that cannot be decoded as UTF-8 have `encoding: "base64"`.
// The query is disabled when either `conversationId` or `path` is null/undefined.

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { withBasePath } from "@/lib/basePath";
import { isDatabricksWorkspace, isHostRoutingEnabled, resolveMediaUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { getSessionHost, isHostKeyless } from "@/lib/sessionHost";
import { readVideoResponse } from "@/lib/videoFetch";
import { readFileViaWorkspaceApi } from "@/lib/workspaceApi";
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

/**
 * The host's directly loadable URL for a workspace file's raw bytes, or null
 * when media must be fetched through the authenticated transport instead.
 *
 * A `<video src>` load sends no request headers, so the session host's routing
 * key rides `?omnigent_slice_key=`, as the terminal attach WebSocket does. A
 * host already proven to route keyless is left unkeyed. With the host unknown,
 * returns null so the load goes through `authenticatedFetch`, which recovers
 * from a wrong-replica miss.
 *
 * :param conversationId: The session/conversation ID, e.g. ``"sess_abc123"``.
 * :param path: Workspace-relative or host-absolute file path.
 */
export function workspaceFileMediaUrl(conversationId: string, path: string): string | null {
  const query: Record<string, string> = { download: "true" };
  const hostId = getSessionHost(conversationId);
  if (isHostRoutingEnabled() && hostId === null) return null;
  if (hostId && !isHostKeyless(hostId)) query.omnigent_slice_key = hostId;
  return resolveMediaUrl(workspaceFileUrl(conversationId, path, query));
}

/**
 * A video's bytes for the player, through the host's authenticated transport,
 * typed with its allowlisted video type and capped at the video payload limit.
 */
export async function fetchWorkspaceVideoBlob(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const res = await authenticatedFetch(
    workspaceFileUrl(conversationId, path, { download: "true" }),
    { signal },
  );
  if (!res.ok) {
    void res.body?.cancel().catch(() => {});
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return readVideoResponse(res, path, { signal });
}

export async function fetchFileContent(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
  options: { scope?: "workspace"; sshTarget?: string | null } = {},
): Promise<FileContentResponse> {
  const { sshTarget, ...query } = options;
  // The workspace API reads only inside the workspace, so it serves either scope.
  if (!browseLocationBase(path)) {
    const viaApi = await readFileViaWorkspaceApi(conversationId, path, signal, sshTarget);
    if (viaApi) return viaApi;
  }
  return fetchFileContentViaRest(conversationId, path, signal, query);
}

/** {@link fetchFileContent} through the REST route only, for a caller that already asked the API. */
export async function fetchFileContentViaRest(
  conversationId: string,
  path: string,
  signal?: AbortSignal,
  query: { scope?: "workspace" } = {},
): Promise<FileContentResponse> {
  const res = await authenticatedFetch(workspaceFileUrl(conversationId, path, query), { signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  // Older servers ignore scope; automatic previews must fail closed there.
  if (query.scope && res.headers.get("X-Omnigent-File-Scope") !== query.scope) {
    throw new Error("Workspace-constrained file reads are unavailable");
  }
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
  await downloadApiFile(url, path.split("/").pop() ?? path);
}

/**
 * Save an app API response's bytes as `filename`; see {@link downloadWorkspaceFile}.
 * `typeImageName` adds an image extension from the fetched type; the link path
 * gets the server's `Content-Disposition` name instead.
 */
async function downloadApiFile(
  url: string,
  filename: string,
  typeImageName = false,
): Promise<void> {
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
  const blob = await res.blob();
  triggerBrowserDownload(blob, typeImageName ? withImageExtension(filename, blob.type) : filename);
}

// The page already holds these bytes, so saving them sends no request.
const IN_MEMORY_IMAGE_SOURCE = /^(?:blob|data):/i;

/** Each savable image type's extensions; the first is the one a name gets. */
const IMAGE_EXTENSIONS_BY_TYPE = new Map<string, readonly string[]>([
  ["image/png", ["png"]],
  ["image/jpeg", ["jpg", "jpeg"]],
  ["image/gif", ["gif"]],
  ["image/webp", ["webp"]],
  ["image/avif", ["avif"]],
  ["image/bmp", ["bmp"]],
  ["image/svg+xml", ["svg"]],
]);

function imageExtensions(type: string): readonly string[] | undefined {
  return IMAGE_EXTENSIONS_BY_TYPE.get(type.split(";")[0].trim().toLowerCase());
}

/**
 * End a save's name in its image type's extension. The name can come from a
 * transcript, so an SVG named `invoice.html` saves as `invoice.html.svg`.
 */
function withImageExtension(name: string, type: string): string {
  const extensions = imageExtensions(type);
  if (!extensions) return name;
  const extension = name.match(/\.([^./\s]+)$/)?.[1].toLowerCase();
  return extension && extensions.includes(extension) ? name : `${name}.${extensions[0]}`;
}

/** The type a `data:` URL declares, e.g. ``"image/png"``; undefined for other sources. */
function dataUrlType(src: string): string | undefined {
  return src.match(/^data:([^;,]*)/i)?.[1];
}

/** Only the app's own API is re-read; anything else would be a new request elsewhere. */
function isAppApiPath(path: string | undefined): path is string {
  return path !== undefined && path.startsWith("/v1/");
}

/** Where to read an on-screen image's bytes from, besides the source it displays. */
export interface ImageDownloadSource {
  /** The app API path the image was read from. */
  apiPath?: string;
  /** The image's bytes, which stay valid after the page revokes its object URL. */
  blob?: Blob;
}

/**
 * Whether {@link downloadImage} can save an on-screen image.
 *
 * :param src: The source the `<img>` displays.
 * :param source: Other places its bytes can be read from.
 */
export function canDownloadImage(
  src: string | undefined,
  { apiPath, blob }: ImageDownloadSource = {},
): boolean {
  // The iOS shell's web view has no download handler, so a save would never land.
  if (src === undefined || isIOSShell()) return false;
  if (blob !== undefined) return true;
  // A browser shows a `data:` image of any `image/*` type it can decode; only
  // types this can name are saved.
  const dataType = dataUrlType(src);
  if (dataType !== undefined) return imageExtensions(dataType) !== undefined;
  return IN_MEMORY_IMAGE_SOURCE.test(src) || isAppApiPath(apiPath);
}

/**
 * Save an on-screen image from held bytes, its `blob:`/`data:` source, or its
 * app API path; never fetches a remote URL.
 *
 * :param src: The source the `<img>` displays.
 * :param filename: The suggested filename for the save dialog.
 * :param source: The held bytes or app API path, e.g. an `apiPath` of
 *     ``"/v1/sessions/s/resources/files/f/content"``.
 */
export async function downloadImage(
  src: string,
  filename: string,
  { apiPath, blob }: ImageDownloadSource = {},
): Promise<void> {
  if (!canDownloadImage(src, { apiPath, blob })) throw new Error("This image can't be downloaded");
  if (blob) {
    triggerBrowserDownload(blob, withImageExtension(filename, blob.type));
    return;
  }
  if (IN_MEMORY_IMAGE_SOURCE.test(src)) {
    clickDownloadLink(src, withImageExtension(filename, dataUrlType(src) ?? ""));
    return;
  }
  if (apiPath) await downloadApiFile(apiPath, filename, true);
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
 * The query for one file's contents, shared by the viewer and prefetch so a
 * prefetched file opens from the cache.
 */
export function fileContentQuery(serverId: string, path: string, sshTarget?: string | null) {
  return {
    queryKey: ["file-content", serverId, path],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      await fileWritesSettled(serverId, path);
      // Cancelled while waiting (a newer write started): don't send the read.
      signal.throwIfAborted();
      return fetchFileContent(serverId, path, signal, { sshTarget });
    },
    staleTime: 5_000,
  };
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
 * :param options.sshTarget: The host's SSH alias, which the desktop may use to
 *     read a host on another machine directly.
 */
export function useFileContent(
  conversationId: string | undefined,
  path: string | null,
  options: { retry?: boolean; visible?: boolean; sshTarget?: string | null } = {},
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
    ...fileContentQuery(serverId ?? "", path ?? "", options.sshTarget),
    enabled: !!serverId && !!path && serveable !== false,
    refetchInterval: visible
      ? (query) => fileContentPollInterval(query.state.data, query.state.status === "error")
      : false,
    // Catch up as soon as the browser tab returns, even within staleTime.
    refetchOnWindowFocus: visible ? "always" : false,
    ...(options.retry === undefined ? {} : { retry: options.retry }),
  });
}
