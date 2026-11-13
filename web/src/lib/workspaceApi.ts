// The workspace API (`omnigent.api.workspace.v1alpha1`): reads a session's
// workspace files through the session's host.
//
// The desktop shell reads straight from the host's local socket with a
// short-lived token the server grants to the session's owner. Other clients,
// and the desktop when that fails, call the same API through the server. Both
// answer exactly like the REST file routes, which stay the fallback whenever
// neither route is enabled or works.

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

// Renew a grant this long before the host would refuse it.
const GRANT_RENEW_MARGIN_MS = 30_000;
// A route that failed for a session is skipped for this long, then tried again.
const ROUTE_RETRY_MS = 5 * 60_000;
// Answers about the request itself, not failures of the route that carried it.
const ANSWER_CODES = new Set([Code.NotFound, Code.InvalidArgument]);

const grants = new Map<string, Promise<DirectGrant>>();
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

async function requestGrant(sessionId: string): Promise<DirectGrant> {
  const res = await authenticatedFetch(`${sessionPath(sessionId)}/direct-access`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { socket_path: string; token: string; expires_at: number };
  return { socketPath: body.socket_path, token: body.token, expiresAtMs: body.expires_at * 1000 };
}

/** The session's current grant; concurrent reads share one request for it. */
async function directGrant(sessionId: string): Promise<DirectGrant> {
  const cached = grants.get(sessionId);
  const grant = cached && (await cached.catch(() => null));
  if (grant && grant.expiresAtMs - GRANT_RENEW_MARGIN_MS > Date.now()) return grant;
  // The first read to find it stale renews it for every read waiting with it.
  if (grants.get(sessionId) === cached) grants.set(sessionId, requestGrant(sessionId));
  return grants.get(sessionId)!;
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
        const response = await send({
          socketPath: grant.socketPath,
          path: new URL(String(input)).pathname,
          headers,
          body: (init?.body as Uint8Array | undefined) ?? new Uint8Array(),
        });
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

async function clientFor(route: Route, sessionId: string): Promise<WorkspaceClient> {
  if (route === "server") return serverClient(sessionId);
  const send = workspaceSocketBridge();
  if (!send) throw new Error("The desktop shell can't reach local sockets");
  return directClient(await directGrant(sessionId), send);
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
    content: text ? new TextDecoder().decode(bytes) : base64Encode(bytes),
    bytes: Number(metadata.size),
    truncated: metadata.truncated,
  };
}

/**
 * Read a workspace file through the workspace API, on the first route that works.
 *
 * Tries the host's local socket first (desktop shell only), then the server.
 * A route that fails is skipped for the session for a while.
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
      return await readFile(await clientFor(route, sessionId), path, signal);
    } catch (error) {
      signal?.throwIfAborted();
      const code = error instanceof ConnectError ? error.code : null;
      if (code !== null && ANSWER_CODES.has(code)) return null;
      if (route === "direct") grants.delete(sessionId);
      // A restarted host refuses the old token; the next read gets a new one.
      if (route === "direct" && code === Code.Unauthenticated) continue;
      failedUntil.set(`${route}:${sessionId}`, Date.now() + ROUTE_RETRY_MS);
    }
  }
  /* oxlint-enable no-await-in-loop */
  return null;
}
