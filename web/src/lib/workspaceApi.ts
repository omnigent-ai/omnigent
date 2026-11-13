// Client for the workspace API (`omnigent.api.workspace.v1alpha1`), which reads
// a session's workspace files through its host; see readFileViaWorkspaceApi.

import { Code, ConnectError, createClient, type Client } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { base64Encode } from "@bufbuild/protobuf/wire";
import {
  ContentEncoding,
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

function routesFor(sessionId: string): Route[] {
  const info = getCachedServerInfo() ?? "loading";
  const routes: Route[] = [];
  if (workspaceSocketBridge() && isFeatureEnabled(info, "workspace_direct_access")) {
    routes.push("direct");
  }
  if (isFeatureEnabled(info, "workspace_api_server_access")) routes.push("server");
  return routes.filter((route) => (failedUntil.get(`${route}:${sessionId}`) ?? 0) <= Date.now());
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
  const body = (await res.json()) as { socket_path: string; token: string; expires_at: number };
  return { socketPath: body.socket_path, token: body.token, expiresAtMs: body.expires_at * 1000 };
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

async function readFileOn(
  route: Route,
  sessionId: string,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse> {
  if (route === "server") return readFile(serverClient(sessionId), path, signal);
  const send = workspaceSocketBridge();
  if (!send) throw new Error("The desktop shell can't reach local sockets");
  const pending = directGrant(sessionId);
  try {
    return await readFile(directClient(await abortable(pending, signal), send), path, signal);
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

/**
 * Read a workspace file through the workspace API, on the first route that works.
 *
 * Tries the host's local socket first (desktop shell only, with a short-lived
 * token the server grants the session's owner), then the server. A route that
 * fails is skipped for the session for a while; REST is the fallback.
 *
 * :param sessionId: The session whose workspace to read.
 * :param path: Workspace-relative path, e.g. ``"src/main.py"``.
 * :returns: The REST route's payload, or null when the caller should read
 *     through REST: no route is enabled or working, or the API answered with
 *     an error, which REST reports in the shape callers expect.
 */
export async function readFileViaWorkspaceApi(
  sessionId: string,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse | null> {
  // Each route's attempt must finish before the next is tried.
  /* oxlint-disable no-await-in-loop */
  for (const route of routesFor(sessionId)) {
    try {
      return await readFileOn(route, sessionId, path, signal);
    } catch (error) {
      signal?.throwIfAborted();
      const code = error instanceof ConnectError ? error.code : null;
      if (code !== null && ANSWER_CODES.has(code)) return null;
      if (code !== null && TRANSIENT_CODES.has(code)) continue;
      // A restarted host refuses the old token; the next read gets a new one.
      if (route === "direct" && code === Code.Unauthenticated) continue;
      failedUntil.set(`${route}:${sessionId}`, Date.now() + ROUTE_RETRY_MS);
    }
  }
  /* oxlint-enable no-await-in-loop */
  return null;
}
