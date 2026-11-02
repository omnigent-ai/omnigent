// Serves a room's built web app read-only so the PO can open it from the
// project bar. It listens on its own port: project code then runs on a
// different origin from the office and cannot call the office's API.

import { createReadStream, existsSync, realpathSync, statSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join, sep } from "node:path";
import { isAllowedHost } from "./http.ts";
import { workspacePath } from "./roomResources.ts";

// Where a project's web entry page usually is, built output first.
const ENTRY_FOLDERS = ["dist", "build", "out", "", "public"];

const PROJECT_COOKIE = "office_preview";
const HOSTS_TTL_MS = 30_000;

const PREVIEW_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".map": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".wasm": "application/wasm",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".mp4": "video/mp4",
  ".txt": "text/plain; charset=utf-8",
};

/** The folder (relative to the workspace) that holds the app's index.html, if any. */
export function findPreviewRoot(workspace: string): string | null {
  for (const folder of ENTRY_FOLDERS) {
    if (existsSync(join(workspace, folder, "index.html"))) return folder;
  }
  return null;
}

/**
 * The file a preview request names, or null when it would leave the app's folder,
 * including through a symlink. A folder, or a route without a file extension
 * (a single-page app's own URL), serves the app's index.html.
 */
export function previewFile(workspace: string, root: string, requestPath: string): string | null {
  const base = join(workspace, root);
  const resolved = workspacePath(base, requestPath);
  if (!resolved) return null;
  let file = resolved.absolute;
  if (!existsSync(file)) {
    if (extname(file)) return null;
    file = join(base, "index.html");
  } else if (statSync(file).isDirectory()) {
    file = join(file, "index.html");
    if (!existsSync(file)) return null;
  }
  const real = realpathSync(file);
  const realBase = realpathSync(base);
  return real === realBase || real.startsWith(realBase + sep) ? real : null;
}

export interface PreviewRoom {
  workspace: string;
  /** The room's host runs on this machine, so its folder is readable here. */
  local: boolean;
}

export interface PreviewOptions {
  port: number;
  resolveRoom: (projectId: string) => Promise<PreviewRoom | null>;
}

function cookieProject(request: IncomingMessage): string | null {
  const match = new RegExp(`(?:^|;\\s*)${PROJECT_COOKIE}=([A-Za-z0-9_-]+)`).exec(request.headers.cookie ?? "");
  return match?.[1] ?? null;
}

export class PreviewService {
  private readonly options: PreviewOptions;
  private readonly rooms = new Map<string, { at: number; room: PreviewRoom | null }>();

  constructor(options: PreviewOptions) {
    this.options = options;
  }

  private async room(projectId: string): Promise<PreviewRoom | null> {
    const cached = this.rooms.get(projectId);
    if (cached && Date.now() - cached.at < HOSTS_TTL_MS) return cached.room;
    const room = await this.options.resolveRoom(projectId).catch(() => null);
    this.rooms.set(projectId, { at: Date.now(), room });
    return room;
  }

  /** The address that opens the room's app, when it has one this machine can serve. */
  async entryUrl(projectId: string): Promise<{ url: string; folder: string } | null> {
    const room = await this.room(projectId);
    if (!room?.local) return null;
    const folder = findPreviewRoot(room.workspace);
    return folder === null ? null : { url: `http://127.0.0.1:${this.options.port}/${projectId}/`, folder };
  }

  async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const send = (status: number, text: string) => {
      response.writeHead(status, { "Content-Type": "text/plain; charset=utf-8" });
      response.end(text);
    };
    if (!isAllowedHost(request.headers.host, [this.options.port])) return send(403, "Forbidden host");
    if (request.method !== "GET" && request.method !== "HEAD") return send(405, "Read-only preview");
    const pathname = decodeURIComponent(new URL(request.url ?? "/", "http://localhost").pathname);
    const [, first = "", ...rest] = pathname.split("/");

    let projectId: string | null = null;
    let filePath = pathname;
    if (/^[A-Za-z0-9_-]+$/.test(first) && (await this.room(first))) {
      if (pathname === `/${first}`) {
        response.writeHead(302, { Location: `/${first}/` });
        return void response.end();
      }
      projectId = first;
      filePath = rest.join("/");
    } else {
      // Root paths such as /assets/app.js belong to the app opened last in this browser.
      projectId = cookieProject(request);
    }
    const room = projectId ? await this.room(projectId) : null;
    if (!projectId || !room?.local) return send(404, "No preview for this address");
    const root = findPreviewRoot(room.workspace);
    const file = root === null ? null : previewFile(room.workspace, root, filePath);
    if (!file) return send(404, "Not found");
    response.writeHead(200, {
      "Content-Type": PREVIEW_TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
      "Set-Cookie": `${PROJECT_COOKIE}=${projectId}; Path=/; HttpOnly; SameSite=Strict`,
    });
    if (request.method === "HEAD") return void response.end();
    createReadStream(file).pipe(response);
  }
}
