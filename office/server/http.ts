// Office HTTP API and static file server. Bound to loopback; every request
// must name a loopback Host, and state-changing requests must come from the
// office's own origin.

import { createReadStream, existsSync, statSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { OfficeRequestError, type BuildingService } from "./building.ts";
import { OmnigentError, type OmnigentClient } from "./omnigentClient.ts";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
const MAX_BODY_BYTES = 64 * 1024;

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
};

/** Rejects DNS-rebinding hosts: only loopback names on the office's port pass. */
export function isAllowedHost(hostHeader: string | undefined, allowedPorts: number[]): boolean {
  if (!hostHeader) return false;
  const match = /^(\[[^\]]+\]|[^:]+)(?::(\d+))?$/.exec(hostHeader);
  if (!match) return false;
  const [, host, port] = match;
  return LOOPBACK_HOSTS.has(host.toLowerCase()) && (port === undefined || allowedPorts.includes(Number(port)));
}

/** State-changing requests must carry an Origin that is the office itself. */
export function isAllowedOrigin(origin: string | undefined, allowedPorts: number[]): boolean {
  if (!origin) return false;
  try {
    const url = new URL(origin);
    return url.protocol === "http:" && isAllowedHost(url.host, allowedPorts);
  } catch {
    return false;
  }
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(JSON.stringify(body));
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  if (!request.headers["content-type"]?.startsWith("application/json")) {
    throw new OfficeRequestError(415, "unsupported_media_type", "Expected application/json");
  }
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new OfficeRequestError(413, "body_too_large", "Request body too large");
    chunks.push(chunk as Buffer);
  }
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
  } catch {
    // fall through
  }
  throw new OfficeRequestError(400, "invalid_json", "Body must be a JSON object");
}

export interface OfficeHandlerOptions {
  building: BuildingService;
  client: OmnigentClient;
  /** Ports the browser may use to reach the office (service and dev server). */
  allowedPorts: number[];
  staticDir: string | null;
}

type RouteHandler = (params: string[], request: IncomingMessage) => Promise<unknown>;

export function createOfficeHandler(options: OfficeHandlerOptions) {
  const { building, client, allowedPorts, staticDir } = options;

  const routes: { method: string; pattern: RegExp; handle: RouteHandler }[] = [
    { method: "GET", pattern: /^\/office\/api\/building$/, handle: async () => building.getSnapshot() },
    {
      method: "POST",
      pattern: /^\/office\/api\/building\/refresh$/,
      handle: async () => building.getSnapshot(true),
    },
    {
      method: "PATCH",
      pattern: /^\/office\/api\/rooms\/([^/]+)$/,
      handle: async ([projectId], request) => {
        const body = await readJsonBody(request);
        return building.updateRoom(projectId, {
          priority: body.priority as never,
          keepUpstairs: body.keepUpstairs as never,
        });
      },
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/rooms\/([^/]+)\/workers$/,
      handle: async ([projectId]) => ({ data: await building.listWorkers(projectId) }),
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/sessions\/([^/]+)$/,
      handle: async ([sessionId]) => client.getSession(sessionId),
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/sessions\/([^/]+)\/items$/,
      handle: async ([sessionId]) => client.listItems(sessionId, { limit: 40 }),
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/sessions\/([^/]+)\/messages$/,
      handle: async ([sessionId], request) => {
        const body = await readJsonBody(request);
        if (typeof body.text !== "string") throw new OfficeRequestError(400, "invalid_text", "text must be a string");
        return building.sendMessage(sessionId, body.text);
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/sessions\/([^/]+)\/stop$/,
      handle: async ([sessionId]) => client.interrupt(sessionId),
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/sessions\/([^/]+)\/elicitations\/([^/]+)\/decision$/,
      handle: async ([sessionId, elicitationId], request) => {
        const body = await readJsonBody(request);
        return building.decide(
          sessionId,
          elicitationId,
          body.action as "accept" | "decline",
          typeof body.feedback === "string" ? body.feedback : undefined,
        );
      },
    },
  ];

  function serveStatic(pathname: string, response: ServerResponse): void {
    if (!staticDir) {
      sendJson(response, 404, { error: { code: "not_found", message: "Run the Vite dev server for the UI" } });
      return;
    }
    const relative = normalize(decodeURIComponent(pathname)).replace(/^([/\\])+/, "");
    let filePath = join(staticDir, relative);
    if (!filePath.startsWith(staticDir + sep) && filePath !== staticDir) {
      sendJson(response, 403, { error: { code: "forbidden", message: "Forbidden" } });
      return;
    }
    if (!existsSync(filePath) || statSync(filePath).isDirectory()) filePath = join(staticDir, "index.html");
    response.writeHead(200, {
      "Content-Type": CONTENT_TYPES[extname(filePath)] ?? "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
    });
    createReadStream(filePath).pipe(response);
  }

  return async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (!isAllowedHost(request.headers.host, allowedPorts)) {
      sendJson(response, 403, { error: { code: "forbidden_host", message: "Host not allowed" } });
      return;
    }
    const method = request.method ?? "GET";
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    if (method !== "GET" && method !== "HEAD" && !isAllowedOrigin(request.headers.origin, allowedPorts)) {
      sendJson(response, 403, { error: { code: "forbidden_origin", message: "Origin not allowed" } });
      return;
    }
    if (!pathname.startsWith("/office/api/")) {
      serveStatic(pathname, response);
      return;
    }
    for (const route of routes) {
      const match = route.pattern.exec(pathname);
      if (!match || route.method !== method) continue;
      try {
        const params = match.slice(1).map((value) => decodeURIComponent(value));
        sendJson(response, 200, await route.handle(params, request));
      } catch (error) {
        if (error instanceof OfficeRequestError || error instanceof OmnigentError) {
          const status = error instanceof OmnigentError && error.status >= 500 ? 502 : error.status;
          sendJson(response, status, { error: { code: error.code ?? "omnigent_error", message: error.message } });
        } else {
          sendJson(response, 500, { error: { code: "internal", message: (error as Error).message } });
        }
      }
      return;
    }
    sendJson(response, 404, { error: { code: "not_found", message: "Unknown office route" } });
  };
}
