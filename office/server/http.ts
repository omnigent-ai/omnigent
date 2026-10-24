// Office HTTP API and static file server. Bound to loopback; every request
// must name a loopback Host, and state-changing requests must come from the
// office's own origin.

import { createReadStream, existsSync, statSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import type { Duplex } from "node:stream";
import { OfficeRequestError, type BuildingService } from "./building.ts";
import { proxyDictation } from "./dictationProxy.ts";
import { handleMcpMessage, officeTools } from "./mcp.ts";
import { OmnigentError, type OmnigentClient } from "./omnigentClient.ts";
import { WorkflowError, normalizeRuntime, type WorkflowService } from "./workflow.ts";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
const MAX_BODY_BYTES = 64 * 1024;
const MAX_MCP_BODY_BYTES = 1024 * 1024;
export const MCP_PATH = "/office/mcp";

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

async function readJson(request: IncomingMessage, limit: number): Promise<unknown> {
  if (!request.headers["content-type"]?.startsWith("application/json")) {
    throw new OfficeRequestError(415, "unsupported_media_type", "Expected application/json");
  }
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    size += (chunk as Buffer).length;
    if (size > limit) throw new OfficeRequestError(413, "body_too_large", "Request body too large");
    chunks.push(chunk as Buffer);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
  } catch {
    throw new OfficeRequestError(400, "invalid_json", "Body must be JSON");
  }
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const parsed = await readJson(request, MAX_BODY_BYTES);
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
  throw new OfficeRequestError(400, "invalid_json", "Body must be a JSON object");
}

function bearerToken(request: IncomingMessage): string {
  const header = request.headers.authorization ?? "";
  return header.startsWith("Bearer ") ? header.slice("Bearer ".length).trim() : "";
}

export interface OfficeHandlerOptions {
  building: BuildingService;
  workflow: WorkflowService;
  client: OmnigentClient;
  /** Ports the browser may use to reach the office (service and dev server). */
  allowedPorts: number[];
  staticDir: string | null;
}

type RouteHandler = (params: string[], request: IncomingMessage) => Promise<unknown>;

export function createOfficeHandler(options: OfficeHandlerOptions) {
  const { building, workflow, client, allowedPorts, staticDir } = options;
  const tools = officeTools(workflow);

  /** Send the PO's own words to a session; these count as human interactions. */
  async function sendAsPo(sessionId: string | null, text: string): Promise<void> {
    if (!sessionId || !text) return;
    await building.sendMessage(sessionId, text);
  }

  const afterWorkflowChange = <T>(value: T): T => {
    building.invalidate();
    return value;
  };

  const routes: { method: string; pattern: RegExp; handle: RouteHandler }[] = [
    { method: "GET", pattern: /^\/office\/api\/building$/, handle: async () => building.getSnapshot() },
    {
      method: "POST",
      pattern: /^\/office\/api\/rooms$/,
      handle: async (_params, request) => {
        const body = await readJsonBody(request);
        const theme = body.theme as { runtime?: Record<string, unknown>; gates?: unknown } | null | undefined;
        const runtime = theme ? { ...theme.runtime, hostId: body.hostId, workspace: body.workspace } : null;
        // Refuse a bad runtime before the project exists, so a failed request leaves no half-made room.
        if (runtime) normalizeRuntime(runtime);
        const { projectId } = await building.createRoom(body);
        if (runtime) await workflow.assignTheme(projectId, { runtime: runtime as never, gates: theme?.gates as never });
        return { projectId, snapshot: await building.getSnapshot(true) };
      },
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/hosts\/([^/]+)\/directories$/,
      handle: async ([hostId], request) => {
        const path = new URL(request.url ?? "/", "http://localhost").searchParams.get("path") ?? "~";
        const listing = await client.listHostDirectory(hostId, path);
        return {
          directories: listing.data.filter((entry) => entry.type === "directory").map(({ name, path: entryPath }) => ({ name, path: entryPath })),
          hasMore: listing.has_more,
        };
      },
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/runtime-options$/,
      handle: async () => {
        const [hosts, harnesses, info] = await Promise.all([
          client.listHosts().catch(() => ({ data: [] })),
          client.listHarnesses().catch(() => ({ data: [] })),
          client.info().catch((): Record<string, unknown> => ({})),
        ]);
        return { hosts, harnesses, dictationAvailable: info.dictation_available === true };
      },
    },
    {
      method: "GET",
      pattern: /^\/office\/api\/rooms\/([^/]+)\/branch$/,
      handle: async ([projectId]) => {
        const branch = workflow.activeBranch(projectId);
        return { branch: branch ? workflow.view(branch.id) : null };
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/rooms\/([^/]+)\/theme$/,
      handle: async ([projectId], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(
          await workflow.assignTheme(projectId, { runtime: body.runtime as never, gates: body.gates as never }),
        );
      },
    },
    {
      method: "PUT",
      pattern: /^\/office\/api\/branches\/([^/]+)\/board$/,
      handle: async ([branchId], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(await workflow.updateBoard(branchId, body.baseRevision, body.requirements));
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/proposals\/([^/]+)\/decision$/,
      handle: async ([branchId, proposalId], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(await workflow.decideProposal(branchId, proposalId, body.action, body.feedback));
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/gates\/([^/]+)\/decision$/,
      handle: async ([branchId, gateId], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(
          await workflow.decideGate(branchId, gateId, { action: body.action, digest: body.digest, feedback: body.feedback }),
        );
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/interview$/,
      handle: async ([branchId], request) => {
        const body = await readJsonBody(request);
        const kickoff = workflow.interviewKickoff(branchId, body.topic);
        await sendAsPo(kickoff.sessionId, kickoff.text);
        return workflow.view(branchId);
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/interviews\/([^/]+)\/answer$/,
      handle: async ([branchId, questionId], request) => {
        const body = await readJsonBody(request);
        const result = await workflow.answerInterview(branchId, questionId, body.answer);
        await sendAsPo(result.sessionId, result.text);
        return afterWorkflowChange(result.view);
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/tasks\/([^/]+)\/complete$/,
      handle: async ([branchId, taskId], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(await workflow.completeTaskAsPo(branchId, taskId, body.summary));
      },
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/stop$/,
      handle: async ([branchId]) => afterWorkflowChange(await workflow.stopBranch(branchId)),
    },
    {
      method: "POST",
      pattern: /^\/office\/api\/branches\/([^/]+)\/roles\/([^/]+)\/rollover$/,
      handle: async ([branchId, role], request) => {
        const body = await readJsonBody(request);
        return afterWorkflowChange(await workflow.rolloverRole(branchId, role, body.reason));
      },
    },
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
      handle: async ([sessionId]) => {
        const page = await client.listItems(sessionId, { limit: 40 });
        // Mark messages the office relayed between agents so they are not shown as the PO's.
        return { ...page, data: page.data.map((item) => (workflow.isRelayedItem(item.id) ? { ...item, office_relayed: true } : item)) };
      },
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
      "Content-Security-Policy":
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; worker-src 'self' blob:",
    });
    createReadStream(filePath).pipe(response);
  }

  /** Role sessions call the office over MCP; their bearer token is their identity. */
  async function handleMcp(request: IncomingMessage, response: ServerResponse): Promise<void> {
    // Browsers never hold a role token; refuse any browser-originated call outright.
    if (request.headers.origin !== undefined) {
      sendJson(response, 403, { error: { code: "forbidden_origin", message: "Origin not allowed" } });
      return;
    }
    const caller = workflow.callerForToken(bearerToken(request));
    if (!caller) {
      sendJson(response, 401, { error: { code: "unauthorized", message: "Unknown or retired role token" } });
      return;
    }
    if (request.method !== "POST") {
      response.writeHead(405, { Allow: "POST" });
      response.end();
      return;
    }
    try {
      const payload = await readJson(request, MAX_MCP_BODY_BYTES);
      const result = await handleMcpMessage(payload, caller, tools);
      building.invalidate();
      if (result === null) {
        response.writeHead(202);
        response.end();
        return;
      }
      sendJson(response, 200, result);
    } catch (error) {
      const status = error instanceof OfficeRequestError ? error.status : 500;
      sendJson(response, status, { jsonrpc: "2.0", id: null, error: { code: -32700, message: (error as Error).message } });
    }
  }

  return async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (!isAllowedHost(request.headers.host, allowedPorts)) {
      sendJson(response, 403, { error: { code: "forbidden_host", message: "Host not allowed" } });
      return;
    }
    const method = request.method ?? "GET";
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    if (pathname === MCP_PATH) {
      await handleMcp(request, response);
      return;
    }
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
        if (error instanceof OfficeRequestError || error instanceof OmnigentError || error instanceof WorkflowError) {
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

export const DICTATION_PATH = "/office/api/dictation/stream";

/** WebSocket upgrades: only the dictation stream, only from the office's own origin. */
export function createUpgradeHandler(options: { client: OmnigentClient; allowedPorts: number[] }) {
  return function upgrade(request: IncomingMessage, socket: Duplex, head: Buffer): void {
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    if (
      pathname !== DICTATION_PATH ||
      !isAllowedHost(request.headers.host, options.allowedPorts) ||
      !isAllowedOrigin(request.headers.origin, options.allowedPorts)
    ) {
      socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      return;
    }
    proxyDictation(request, socket, head, options.client.baseUrl, options.client.authHeaders);
  };
}
