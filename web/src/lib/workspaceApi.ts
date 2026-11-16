// Client for the workspace API (`omnigent.api.workspace.v1alpha1`), which reads
// a session's workspace files through its host; see readFileViaWorkspaceApi.

import { Code, ConnectError, createClient, type Client } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { base64Encode } from "@bufbuild/protobuf/wire";
import {
  ContentEncoding,
  EntryType,
  WorkspaceService,
  type FileMetadata,
} from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import type { FileContentResponse } from "@/hooks/useFileContent";
import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { workspaceSocketBridge, type WorkspaceSocketRequest } from "@/lib/nativeBridge";

type WorkspaceClient = Client<typeof WorkspaceService>;
type Route = "direct" | "server";

/** The server's grant to read one session's workspace from its host's socket. */
interface DirectGrant {
  hostId: string;
  socketPath: string;
  token: string;
  /** When the host stops accepting the token, in this machine's clock (the host's). */
  expiresAtMs: number;
}

/** A session's grant request, and the grant once it arrives. */
interface GrantEntry {
  pending: Promise<DirectGrant>;
  grant?: DirectGrant;
}

// Renew a grant this long before the host would refuse it.
const GRANT_RENEW_MARGIN_MS = 30_000;
// A grant request that takes longer is abandoned, so the next read asks again.
const GRANT_TIMEOUT_MS = 10_000;
// A route that failed for a session is skipped for this long, then tried again.
const ROUTE_RETRY_MS = 5 * 60_000;
// Answers about the request itself, not failures of the route that carried it.
const ANSWER_CODES = new Set([Code.NotFound, Code.InvalidArgument]);
// The host is briefly away, e.g. reconnecting: try the next route, keep this one.
const TRANSIENT_CODES = new Set([Code.Unavailable]);

const grants = new Map<string, GrantEntry>();
const failedUntil = new Map<string, number>();

/** A failed direct route is retried as soon as a read brings an SSH alias it didn't have. */
function routeKey(route: Route, sessionId: string, sshTarget?: string | null): string {
  return route === "direct" ? `direct:${sessionId}:${sshTarget ?? ""}` : `${route}:${sessionId}`;
}

function routesFor(sessionId: string, sshTarget?: string | null): Route[] {
  const info = getCachedServerInfo() ?? "loading";
  const routes: Route[] = [];
  if (workspaceSocketBridge() && isFeatureEnabled(info, "workspace_direct_access")) {
    routes.push("direct");
  }
  if (isFeatureEnabled(info, "workspace_api_server_access")) routes.push("server");
  return routes.filter(
    (route) => (failedUntil.get(routeKey(route, sessionId, sshTarget)) ?? 0) <= Date.now(),
  );
}

function sessionPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/workspace`;
}

/** Settle with `promise`, or reject as soon as `signal` aborts. */
function abortable<T>(promise: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    void promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

async function requestGrant(sessionId: string): Promise<DirectGrant> {
  const res = await authenticatedFetch(`${sessionPath(sessionId)}/direct-access`, {
    method: "POST",
    signal: AbortSignal.timeout(GRANT_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as {
    host_id: string;
    socket_path: string;
    token: string;
    expires_at: number;
  };
  return {
    hostId: body.host_id,
    socketPath: body.socket_path,
    token: body.token,
    expiresAtMs: body.expires_at * 1000,
  };
}

/** The session's current grant; concurrent reads share one request for it. */
function directGrant(sessionId: string): Promise<DirectGrant> {
  const cached = grants.get(sessionId);
  if (cached && (!cached.grant || cached.grant.expiresAtMs - GRANT_RENEW_MARGIN_MS > Date.now())) {
    return cached.pending;
  }
  const entry: GrantEntry = { pending: requestGrant(sessionId) };
  entry.pending.then(
    (grant) => {
      entry.grant = grant;
    },
    () => {
      if (grants.get(sessionId) === entry) grants.delete(sessionId);
    },
  );
  grants.set(sessionId, entry);
  return entry.pending;
}

function directClient(
  grant: DirectGrant,
  send: (request: WorkspaceSocketRequest) => Promise<{
    status: number;
    headers: Record<string, string>;
    body: Uint8Array;
  }>,
  sshTarget?: string | null,
): WorkspaceClient {
  return createClient(
    WorkspaceService,
    createConnectTransport({
      // Only the path reaches the socket; the shell picks the socket.
      baseUrl: "http://workspace.socket",
      useBinaryFormat: true,
      fetch: async (input, init) => {
        const headers = Object.fromEntries(new Headers(init?.headers).entries());
        headers.authorization = `Bearer ${grant.token}`;
        // The shell can't cancel its call, so a cancelled read just stops waiting.
        const response = await abortable(
          send({
            hostId: grant.hostId,
            ...(sshTarget ? { sshTarget } : {}),
            socketPath: grant.socketPath,
            path: new URL(String(input)).pathname,
            headers,
            body: (init?.body as Uint8Array | undefined) ?? new Uint8Array(),
          }),
          init?.signal,
        );
        return new Response(response.body as Uint8Array<ArrayBuffer>, {
          status: response.status,
          headers: response.headers,
        });
      },
    }),
  );
}

function serverClient(sessionId: string): WorkspaceClient {
  return createClient(
    WorkspaceService,
    createConnectTransport({
      baseUrl: sessionPath(sessionId),
      useBinaryFormat: true,
      // Session-scoped, so it rides the host's routing key like the file routes.
      fetch: (input, init) => authenticatedFetch(input, init),
    }),
  );
}

async function callOn<T>(
  route: Route,
  sessionId: string,
  call: (client: WorkspaceClient) => Promise<T>,
  signal?: AbortSignal,
  sshTarget?: string | null,
): Promise<T> {
  if (route === "server") return call(serverClient(sessionId));
  const send = workspaceSocketBridge();
  if (!send) throw new Error("The desktop shell can't reach local sockets");
  const pending = directGrant(sessionId);
  try {
    const grant = await abortable(pending, signal);
    return await call(directClient(grant, send, sshTarget));
  } catch (error) {
    // Drop only the grant this read used; a parallel read may have renewed it.
    if (!signal?.aborted && grants.get(sessionId)?.pending === pending) grants.delete(sessionId);
    throw error;
  }
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const bytes = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes;
}

async function readFile(
  client: WorkspaceClient,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse> {
  let metadata: FileMetadata | undefined;
  const chunks: Uint8Array[] = [];
  for await (const message of client.readFile({ path }, { signal })) {
    metadata ??= message.metadata;
    chunks.push(message.data);
  }
  if (!metadata) throw new ConnectError("The workspace API sent no file metadata", Code.Internal);
  const bytes = concat(chunks);
  // The same payload the REST route sends, so every viewer reads it unchanged.
  const text = metadata.encoding === ContentEncoding.UTF8;
  return {
    object: "session.environment.filesystem.file_content",
    path,
    content_type: metadata.contentType || null,
    encoding: text ? "utf-8" : "base64",
    // Keep a leading BOM, as REST does.
    content: text
      ? new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes)
      : base64Encode(bytes),
    bytes: Number(metadata.size),
    truncated: metadata.truncated,
  };
}

/** One directory entry, in the REST listing's shape. */
export interface ListedEntry {
  id: string;
  name: string;
  path: string;
  type: "file" | "directory";
  bytes: number | null;
  modified_at: number | null;
}

const LIST_PAGE_SIZE = 1000;

async function listDirectory(
  client: WorkspaceClient,
  path: string,
  maxEntries: number,
  signal?: AbortSignal,
): Promise<{ data: ListedEntry[]; hasMore: boolean }> {
  const data: ListedEntry[] = [];
  let pageToken = "";
  // Each page needs the token the previous one returned.
  /* oxlint-disable no-await-in-loop */
  do {
    const page = await client.listDirectory(
      { path, pageSize: Math.min(LIST_PAGE_SIZE, maxEntries - data.length), pageToken },
      { signal },
    );
    for (const entry of page.entries) {
      data.push({
        // The REST listing's entry id is its path.
        id: entry.path,
        name: entry.name,
        path: entry.path,
        type: entry.type === EntryType.DIRECTORY ? "directory" : "file",
        bytes: entry.size === undefined ? null : Number(entry.size),
        modified_at: entry.modifyTime ? Number(entry.modifyTime.seconds) : null,
      });
    }
    pageToken = page.nextPageToken;
  } while (pageToken && data.length < maxEntries);
  /* oxlint-enable no-await-in-loop */
  return { data, hasMore: pageToken !== "" };
}

/**
 * Call the workspace API on the first route that works, or return null so the
 * caller uses REST.
 *
 * Tries the host's local socket first (desktop shell only, with a short-lived
 * token the server grants the session's owner), then the server. A route that
 * fails is skipped for the session for a while. An error answer also returns
 * null, since REST reports it in the shape callers expect.
 */
async function viaWorkspaceApi<T>(
  sessionId: string,
  call: (client: WorkspaceClient) => Promise<T>,
  signal?: AbortSignal,
  sshTarget?: string | null,
): Promise<T | null> {
  // Each route's attempt must finish before the next is tried.
  /* oxlint-disable no-await-in-loop */
  for (const route of routesFor(sessionId, sshTarget)) {
    try {
      return await callOn(route, sessionId, call, signal, sshTarget);
    } catch (error) {
      signal?.throwIfAborted();
      const code = error instanceof ConnectError ? error.code : null;
      if (code !== null && ANSWER_CODES.has(code)) return null;
      if (code !== null && TRANSIENT_CODES.has(code)) continue;
      // A restarted host refuses the old token; the next read gets a new one.
      if (route === "direct" && code === Code.Unauthenticated) continue;
      failedUntil.set(routeKey(route, sessionId, sshTarget), Date.now() + ROUTE_RETRY_MS);
    }
  }
  /* oxlint-enable no-await-in-loop */
  return null;
}

/**
 * List a workspace directory through the workspace API, in the REST listing's
 * shape and order (ascending by path), reading pages up to `maxEntries`.
 *
 * :param sessionId: The session whose workspace to list.
 * :param path: Workspace-relative directory, ``""`` for the root.
 * :param maxEntries: Stop after this many entries, e.g. ``1000``.
 * :returns: The entries and whether more remain, or null when the caller
 *     should list through REST (see :func:`readFileViaWorkspaceApi`).
 */
export function listDirectoryViaWorkspaceApi(
  sessionId: string,
  path: string,
  maxEntries: number,
  signal?: AbortSignal,
): Promise<{ data: ListedEntry[]; hasMore: boolean } | null> {
  return viaWorkspaceApi(
    sessionId,
    (client) => listDirectory(client, path, maxEntries, signal),
    signal,
  );
}

/**
 * Read a workspace file through the workspace API, on the first route that
 * works (see :func:`viaWorkspaceApi`).
 *
 * :param sessionId: The session whose workspace to read.
 * :param path: Workspace-relative path, e.g. ``"src/main.py"``.
 * :param sshTarget: An SSH alias the desktop may try for a host on another
 *     machine it has no destination for, e.g. from the host's provider.
 * :returns: The REST route's payload, or null when the caller should read
 *     through REST: no route is enabled or working, or the API answered with
 *     an error.
 */
export async function readFileViaWorkspaceApi(
  sessionId: string,
  path: string,
  signal?: AbortSignal,
  sshTarget?: string | null,
): Promise<FileContentResponse | null> {
  return viaWorkspaceApi(sessionId, (client) => readFile(client, path, signal), signal, sshTarget);
}
