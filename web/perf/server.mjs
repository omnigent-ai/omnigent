// Serve the built app and synthetic API responses on loopback.

import http from "node:http";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const here = path.dirname(fileURLToPath(import.meta.url));

// ── Args ─────────────────────────────────────────────────────
const args = process.argv.slice(2);
function argOf(name, fallback) {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
}
const PORT = Number(argOf("--port", 5299));
const DIST = path.resolve(here, argOf("--dist", "dist"));
const LOG_PATH = path.resolve(argOf("--out", path.join(here, "out")), "unmatched.log");
const latencyArg = argOf("--latency-ms", "0");
if (!/^\d+(?:-\d+)?$/.test(latencyArg)) throw new Error("--latency-ms must be a nonnegative number or min-max");
const [minLatency, maxLatency = minLatency] = latencyArg.split("-").map(Number);
if (minLatency > maxLatency) throw new Error("--latency-ms minimum exceeds maximum");
fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
const logStream = fs.createWriteStream(LOG_PATH, { flags: "a" });

// ── Fixtures ─────────────────────────────────────────────────
const FIX = path.join(here, "fixtures");
function readJson(name) {
  return JSON.parse(fs.readFileSync(path.join(FIX, name), "utf8"));
}
const sessionsList = readJson("sessions.json");
const tree = readJson("tree.json");
const changes = readJson("changes.json");
const bigFile = fs.readFileSync(path.join(FIX, "bigfile.txt"), "utf8");
const hugeFile = fs.readFileSync(path.join(FIX, "hugefile.txt"), "utf8");
// Deterministic per-path content: paths containing "huge" (or an odd hash) get
// the ~5000-line file, the rest the 1400-line one.
function fileContentFor(filePath) {
  let h = 0;
  for (let i = 0; i < filePath.length; i++) h = (h * 31 + filePath.charCodeAt(i)) | 0;
  return filePath.includes("huge") || Math.abs(h) % 2 === 1 ? hugeFile : bigFile;
}

const itemsBySession = new Map();
for (const s of sessionsList) {
  const key = s.id.startsWith("conv_perf_f") ? s.id : s.id.at(-1);
  itemsBySession.set(s.id, readJson(`items_${key}.json`));
}
const sessionById = new Map(sessionsList.map((s) => [s.id, s]));

// A short unique marker per heavy session, present in its newest user message,
// so the driver can verify the target transcript is on screen.
const sessionMarkers = new Map();
for (const [id, items] of itemsBySession) {
  const lastUser = [...items].reverse().find((it) => it.type === "message" && it.role === "user");
  const marker = `SESSMARK-${id.slice(-4).toUpperCase()}`;
  if (lastUser) {
    lastUser.content[0].text = `${marker} ${lastUser.content[0].text}`;
  }
  sessionMarkers.set(id, marker);
}

// ── Helpers ──────────────────────────────────────────────────
function sendJson(res, status, body, extraHeaders) {
  const buf = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": buf.length,
    ...extraHeaders,
  });
  res.end(buf);
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(""));
  });
}

function logUnmatched(method, url) {
  const line = `${new Date().toISOString()} ${method} ${url}\n`;
  logStream.write(line);
}

const counters = { requests: 0, unmatched: 0 };
const streamOpens = new Map();
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".map": "application/json",
  ".wasm": "application/wasm",
  ".ttf": "font/ttf",
};

function serveStatic(req, res, pathname) {
  let rel = decodeURIComponent(pathname);
  if (rel === "/" || rel === "") rel = "/index.html";
  const file = path.normalize(path.join(DIST, rel));
  if (!file.startsWith(DIST + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    // SPA fallback: any unknown non-asset path serves index.html.
    const index = path.join(DIST, "index.html");
    if (fs.existsSync(index)) {
      const buf = fs.readFileSync(index);
      res.writeHead(200, { "content-type": MIME[".html"], "content-length": buf.length });
      res.end(buf);
      return;
    }
    res.writeHead(404).end("not built");
    return;
  }
  const ext = path.extname(file).toLowerCase();
  const immutable = rel.startsWith("/assets/");
  res.writeHead(200, {
    "content-type": MIME[ext] ?? "application/octet-stream",
    "content-length": fs.statSync(file).size,
    "cache-control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
  });
  fs.createReadStream(file).pipe(res);
}

// ── Mock API state ───────────────────────────────────────────
const NOW = Math.floor(Date.now() / 1000);

const SERVER_INFO = {
  single_user: true,
  accounts_enabled: false,
  login_url: null,
  needs_setup: false,
  managed_sandboxes_enabled: false,
  durable_initial_input: false,
  sharing_mode: "on",
  public_sharing_enabled: false,
  server_version: "perf-mock-0.0.0",
  smart_routing_enabled: false,
  features: {
    "list_directory.collapse_single_child_directories": true,
  },
  harness_install_enabled: false,
  dictation_available: false,
  automations_enabled: false,
  session_search_scope: false,
  session_rewind: false,
  session_pull_requests: false,
  session_journal: true,
  workflows: false,
  admin_view: false,
  session_event_types: [],
  sandbox_providers: [],
  enabled_connections: [],
  installable_harnesses: [],
  branding: null,
};

const HOSTS_RESPONSE = { hosts: [] };

const AGENTS_RESPONSE = {
  agents: [
    {
      id: "ag_perf",
      name: "example-agent",
      display_name: "Example Agent",
      description: "Mocked agent for the performance baseline",
      harness: "example",
      builtin: true,
      editable: false,
      icon: null,
    },
  ],
};

const RUNNERS_RESPONSE = {
  runners: [
    { runner_id: "runner_perf", online: true, agent_id: "ag_perf", session_id: null, label: "perf-runner" },
  ],
};

const PROJECTS_RESPONSE = {
  projects: [
    {
      id: "proj_perf",
      name: "example-project",
      color: "blue",
      session_count: 20,
    },
  ],
  order: ["proj_perf"],
};

function sessionSnapshot(s) {
  return {
    ...s,
    llm_model: "example-model",
    context_window: 200000,
    reasoning_effort: null,
    usage_included: true,
    total_cost_usd: 0,
    cost_hidden: false,
    todos: [],
    background_task_count: 0,
    pending_elicitations: [],
    pending_inputs: [],
    failed_inputs: [],
    queued_items: [],
    items: [],
    inference_configured: true,
    inference_error: null,
    terminal_pending: false,
    sandbox_status: null,
    sandbox_profile: null,
    mcp_startup: null,
    active_response_id: null,
    terminal_launch_args: null,
    git_branch: null,
    side_chat_parent_id: null,
    side_chat_allowed: null,
    share_workspace_files: true,
    last_total_tokens: 42000,
    model_override: null,
    cost_control_mode_override: null,
    subagent_routing_override: null,
    parent_session_id: null,
    sub_agent_name: null,
    kind: "default",
    model_options: [],
    side_chat_settings_supported: false,
  };
}

// items page: `order=desc` returns newest-first; `after` = oldest already-loaded id.
function sliceItems(items, url) {
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 100), 500);
  const order = url.searchParams.get("order") ?? "desc";
  const after = url.searchParams.get("after");
  let list = items;
  if (after) {
    const idx = list.findIndex((it) => it.id === after);
    if (idx >= 0) list = order === "desc" ? list.slice(0, idx) : list.slice(idx + 1);
  }
  const ascending = order === "asc";
  const window = ascending ? list.slice(0, limit) : list.slice(-limit).reverse();
  const hasMore = after
    ? ascending
      ? list.length > limit
      : list.length > limit
    : items.length > limit;
  return {
    object: "list",
    data: window,
    first_id: window[0]?.id ?? null,
    last_id: window.at(-1)?.id ?? null,
    has_more: hasMore,
  };
}

// ── Connect-RPC (workspace) handling ─────────────────────────
// Unary methods: POST /v1/sessions/{id}/workspace/{Service}/{Method}
//   request/response bodies are protojson (camelCase).
// Server-stream methods (ReadFile, SearchText): Connect streaming envelope —
//   each message: 1 flag byte (0 = data) + 4-byte BE length + JSON message.
function connectEnvelope(obj) {
  const payload = Buffer.from(JSON.stringify(obj));
  const head = Buffer.alloc(5);
  head.writeUInt8(0, 0);
  head.writeUInt32BE(payload.length, 1);
  return Buffer.concat([head, payload]);
}

function parseConnectRequest(body) {
  // Unary Connect-JSON requests carry a plain JSON object; streaming requests
  // carry envelopes. The harness only needs unary requests plus streamed reads.
  if (!body) return {};
  if (body[0] === 0 && body.length > 5) {
    const len = body.readUInt32BE(1);
    try {
      return JSON.parse(body.subarray(5, 5 + len).toString("utf8"));
    } catch {
      return {};
    }
  }
  try {
    return JSON.parse(body.toString("utf8"));
  } catch {
    return {};
  }
}

function workspaceMethod(res, method, body) {
  const req = parseConnectRequest(body);
  const json = (obj) => {
    const buf = Buffer.from(JSON.stringify(obj));
    res.writeHead(200, { "content-type": "application/json", "content-length": buf.length });
    res.end(buf);
  };
  const stream = (objs) => {
    const chunks = objs.map(connectEnvelope);
    const end = connectEnvelope({}); // end-of-stream marker carries no trailers
    const buf = Buffer.concat([...chunks, end]);
    res.writeHead(200, { "content-type": "application/connect+json", "content-length": buf.length });
    res.end(buf);
  };
  switch (method) {
    case "GetCapabilities":
      json({
        features: ["git", "list_directory.collapse_single_child_directories", "search_files.fuzzy", "content_search"],
      });
      return;
    case "ListDirectory": {
      const prefix = (req.path ?? "").replace(/^\/+|\/+$/g, "");
      const pageSize = Number(req.pageSize ?? 0) || 500;
      const token = Number(req.pageToken ?? 0) || 0;
      const matched = prefix
        ? tree.filter((e) => e.path === prefix || e.path.startsWith(prefix + "/"))
        : tree;
      // Direct children of `prefix` only.
      const depth = prefix ? prefix.split("/").length : 0;
      const children = [];
      for (const e of matched) {
        const segs = e.path.split("/");
        if (segs.length === depth + 1) children.push(e);
      }
      const page = children.slice(token, token + pageSize);
      json({ entries: page, next_page_token: token + pageSize < children.length ? String(token + pageSize) : "" });
      return;
    }
    case "StatPath": {
      const p = (req.path ?? "").replace(/^\/+|\/+$/g, "");
      const entry = tree.find((e) => e.path === p);
      if (!entry) {
        json({ exists: false });
        return;
      }
      json({
        exists: true,
        entry: { name: entry.name, path: entry.path, type: entry.type, size: entry.type === "ENTRY_TYPE_FILE" ? String(entry.size ?? 0) : undefined },
      });
      return;
    }
    case "ReadFile": {
      const content = fileContentFor(req.path ?? "");
      stream([
        {
          metadata: { path: req.path ?? "", size: String(content.length), modify_time: { seconds: String(NOW) } },
        },
        { chunk: { data: Buffer.from(content, "utf8").toString("base64"), encoding: "CONTENT_ENCODING_UNSPECIFIED" } },
      ]);
      return;
    }
    case "SearchFiles": {
      const q = (req.query ?? "").toLowerCase();
      const results = tree
        .filter((e) => e.type === "ENTRY_TYPE_FILE" && e.path.toLowerCase().includes(q))
        .slice(0, req.limit ?? 50)
        .map((e) => ({ path: e.path, entry_type: e.type, score: 1 }));
      json({ results, truncated: false });
      return;
    }
    case "ResolveWorkingCopyBase":
      json({
        working_copy_base: { kind: "COMPARISON_KIND_LAST_COMMIT", head: "HEAD", base: "origin/main", head_is_working_copy: true },
      });
      return;
    case "ListWorkingCopyChanges":
      json({ files: changes, truncated: false });
      return;
    case "ListRepositoryRefs":
      json({ branches: [{ name: "main", commit: "a".repeat(40) }, { name: "perf-work", commit: "b".repeat(40) }] });
      return;
    case "ListBranchCommits":
      json({ commits: [{ sha: "a".repeat(40), summary: "Example commit", author: "dev@example.com", time: { seconds: String(NOW) } }] });
      return;
    case "ResolveRepositoryContext":
      json({ root: "/home/dev/example-project", default_branch: "main" });
      return;
    default:
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ code: "unimplemented", message: `mock: ${method} not implemented` }));
  }
}

// ── SSE streams ──────────────────────────────────────────────
// sessionId → Set of open SSE responses (the app opens one per bound session).
const sseStreams = new Map();
// Injected streaming turns, keyed by a control token.
const activeTurns = new Map();

function sseWrite(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function openSse(res, sessionId) {
  streamOpens.set(sessionId, (streamOpens.get(sessionId) ?? 0) + 1);
  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  res.write(": connected\n\n");
  let set = sseStreams.get(sessionId);
  if (!set) {
    set = new Set();
    sseStreams.set(sessionId, set);
  }
  set.add(res);
  const hb = setInterval(() => res.write(": hb\n\n"), 15000);
  res.on("close", () => {
    clearInterval(hb);
    set.delete(res);
  });
}

function injectStreamTurn(sessionId, opts) {
  const set = sseStreams.get(sessionId);
  if (!set || set.size === 0) return { ok: false, error: "no open stream" };
  const durationMs = opts.durationMs ?? 10000;
  const eventsPerSec = opts.eventsPerSec ?? 20;
  const words = "queue widget handler pipeline snapshot parser session payload buffer ticker renderer".split(" ");
  const total = Math.round((durationMs / 1000) * eventsPerSec);
  const token = randomUUID();
  const responseId = `resp_stream_${Date.now()}`;
  const callId = `call_stream_${Date.now()}`;
  for (const res of set) {
    sseWrite(res, "response.created", { response: { id: responseId, status: "in_progress", model: "example-model", conversation: { id: sessionId } } });
    sseWrite(res, "response.in_progress", { response: { id: responseId, status: "in_progress", model: "example-model" } });
  }
  // Schedule: 55% native text deltas (messageId → applyLiveDelta path),
  // then a tool call with 30% tool_output deltas, then the tail as text.
  const textEnd = Math.floor(total * 0.55);
  const toolEnd = Math.floor(total * 0.85);
  let i = 0;
  const iv = setInterval(() => {
    const n = i++;
    for (const res of set) {
      const word = words[n % words.length];
      if (n < textEnd) {
        sseWrite(res, "response.output_text.delta", {
          delta: (n % 12 === 11 ? "\n" : n % 5 === 4 ? " " : word) + (n % 7 === 3 ? `-${word}` : ""),
          message_id: `msg_${responseId}`,
          index: n,
        });
      } else if (n === textEnd) {
        sseWrite(res, "response.output_item.done", {
          item: {
            id: `itm_stream_call_${responseId}`,
            type: "function_call",
            response_id: responseId,
            status: "in_progress",
            name: "run_command",
            arguments: JSON.stringify({ command: "pnpm test --filter web" }),
            call_id: callId,
          },
        });
      } else if (n < toolEnd) {
        sseWrite(res, "response.function_call_output.delta", {
          call_id: callId,
          delta: `${word} step=${n} status=ok\n`,
        });
      } else if (n === toolEnd) {
        sseWrite(res, "response.output_item.done", {
          item: {
            id: `itm_stream_out_${responseId}`,
            type: "function_call_output",
            response_id: responseId,
            status: "completed",
            call_id: callId,
            output: `tool output settled after ${toolEnd - textEnd} deltas`,
          },
        });
      } else {
        sseWrite(res, "response.output_text.delta", {
          delta: (n % 5 === 4 ? " " : word),
          message_id: `msg_${responseId}`,
          index: n,
        });
      }
    }
    if (n >= total) {
      for (const res of set) {
        sseWrite(res, "response.output_item.done", {
          item: {
            id: `itm_stream_${responseId}`,
            type: "message",
            response_id: responseId,
            status: "completed",
            role: "assistant",
            content: [{ type: "output_text", text: `Streamed ${total} deltas for the perf streaming scenario.` }],
          },
        });
        sseWrite(res, "response.completed", { response: { id: responseId, status: "completed", model: "example-model" } });
      }
      clearInterval(iv);
      activeTurns.delete(token);
    }
  }, 1000 / eventsPerSec);
  activeTurns.set(token, { stop: () => clearInterval(iv) });
  return { ok: true, token, total_events: total };
}

// ── Minimal WebSocket (RFC 6455 subset) for /v1/sessions/updates ──
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
function wsAccept(key) {
  return createHash("sha1").update(key + WS_GUID).digest("base64");
}
function wsFrame(str) {
  const payload = Buffer.from(str);
  const len = payload.length;
  let head;
  if (len < 126) {
    head = Buffer.from([0x81, len]);
  } else if (len < 65536) {
    head = Buffer.alloc(4);
    head[0] = 0x81;
    head[1] = 126;
    head.writeUInt16BE(len, 2);
  } else {
    head = Buffer.alloc(10);
    head[0] = 0x81;
    head[1] = 127;
    head.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([head, payload]);
}
function wsAttach(req, socket, sendFrame) {
  const key = req.headers["sec-websocket-key"];
  if (!key) {
    socket.destroy();
    return;
  }
  socket.write(
    "HTTP/1.1 101 Switching Protocols\r\n" +
      "Upgrade: websocket\r\n" +
      "Connection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${wsAccept(key)}\r\n` +
      "\r\n",
  );
  sendFrame(JSON.stringify({ type: "snapshot", items: sessionsList }));
  const hb = setInterval(() => sendFrame(JSON.stringify({ type: "heartbeat" })), 25000);
  // Consume client frames enough to ignore them (watch-set updates, pongs).
  socket.on("data", () => {});
  socket.on("close", () => clearInterval(hb));
  socket.on("error", () => clearInterval(hb));
}

// ── Server ───────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  counters.requests++;
  const url = new URL(req.url, "http://localhost");
  const p = url.pathname;

  try {
    // Driver control plane.
    if (p.startsWith("/__perf__/")) {
      if (p === "/__perf__/counters") return sendJson(res, 200, { ...counters, streamOpens: Object.fromEntries(streamOpens), activeStreams: Object.fromEntries([...sseStreams].map(([id, streams]) => [id, streams.size])) });
      if (p === "/__perf__/reset") {
        counters.requests = 0;
        counters.unmatched = 0;
        return sendJson(res, 200, { ok: true });
      }
      if (p === "/__perf__/stream/start" && req.method === "POST") {
        const body = await readBody(req);
        let opts = {};
        try {
          opts = JSON.parse(body || "{}");
        } catch {
          opts = {};
        }
        const out = injectStreamTurn(opts.session ?? "conv_perf_a", {
          durationMs: opts.duration_ms,
          eventsPerSec: opts.events_per_sec,
          deltaChars: opts.delta_chars,
        });
        return sendJson(res, out.ok ? 200 : 409, out);
      }
      if (p === "/__perf__/stream/stop" && req.method === "POST") {
        const body = await readBody(req);
        const { token } = JSON.parse(body || "{}");
        const turn = activeTurns.get(token);
        if (turn) {
          turn.stop();
          activeTurns.delete(token);
          return sendJson(res, 200, { ok: true });
        }
        return sendJson(res, 404, { ok: false });
      }
      // App-native file open: push `ui.open_file` over the session's live SSE
      // stream (the same event a runner sends), so the workspace viewer opens
      // through the app's own handler.
      if (p === "/__perf__/openfile" && req.method === "POST") {
        const body = JSON.parse((await readBody(req)) || "{}");
        const set = sseStreams.get(body.session);
        if (!set || set.size === 0) return sendJson(res, 409, { ok: false, error: "no open stream" });
        for (const sseRes of set) sseWrite(sseRes, "ui.open_file", { path: body.path });
        return sendJson(res, 200, { ok: true, path: body.path });
      }
      return sendJson(res, 404, { ok: false });
    }

    // Static app.
    if (!p.startsWith("/v1/") && !p.startsWith("/health") && !p.startsWith("/auth") && !p.startsWith("/.well-known")) {
      return serveStatic(req, res, p);
    }

    // Delay API responses, but not assets or harness control requests.
    if (maxLatency && !p.endsWith("/stream")) await delay(minLatency + Math.random() * (maxLatency - minLatency));

    // ── REST API ──
    if (p === "/.well-known/omnigent.json") return sendJson(res, 200, { version: "perf-mock", api: "v1" });

    if (p === "/health") {
      const ids = (url.searchParams.get("session_ids") ?? "").split(",").filter(Boolean);
      const sessions = {};
      for (const id of ids) {
        sessions[id] = {
          runner_online: sessionById.has(id),
          host_online: null,
          host_version: null,
        };
      }
      return sendJson(res, 200, { sessions, healthy: true });
    }

    if (p === "/v1/info") return sendJson(res, 200, SERVER_INFO);
    if (p === "/v1/me") return sendJson(res, 200, { user_id: "dev@example.com", is_admin: false });
    if (p === "/v1/hosts") return sendJson(res, 200, HOSTS_RESPONSE);
    if (p === "/v1/agents") return sendJson(res, 200, AGENTS_RESPONSE);
    if (p === "/v1/runners") return sendJson(res, 200, RUNNERS_RESPONSE);
    if (p === "/v1/projects") return sendJson(res, 200, PROJECTS_RESPONSE);
    if (p === "/v1/projects/order") return sendJson(res, 200, { order: ["proj_perf"] });
    if (p === "/v1/sharing") return sendJson(res, 200, { mode: "on", public_sharing_enabled: false });
    if (p === "/v1/policies") return sendJson(res, 200, { policies: [] });
    if (p === "/v1/workflows") return sendJson(res, 200, { workflows: [] });
    if (p === "/v1/scheduled-tasks") return sendJson(res, 200, { tasks: [], next_page_token: null });
    if (p === "/v1/scheduled-tasks/default-timezone") return sendJson(res, 200, { timezone: "UTC" });
    if (p === "/v1/harnesses") {
      return sendJson(res, 200, {
        data: [{ id: "example", label: "Example", capabilities: {} }],
        setup_steps: {},
      });
    }
    if (p === "/v1/extensions") return sendJson(res, 200, { object: "list", data: [] });
    if (p === "/v1/sessions/projects") {
      return sendJson(res, 200, [
        { id: "proj_perf", name: "example-project", icon: null, muted: false },
      ]);
    }

    if (p === "/v1/sessions" && req.method === "GET") {
      const list = sessionsList.map((s) => ({ ...s }));
      return sendJson(res, 200, {
        object: "list",
        data: list,
        first_id: list[0]?.id ?? null,
        last_id: list.at(-1)?.id ?? null,
        has_more: false,
      });
    }

    // Session-scoped routes.
    const m = p.match(/^\/v1\/sessions\/([^/]+)(\/.*)?$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const sub = m[2] ?? "";
      if (sub.includes("/resources/") || sub.includes("/workspace/")) {
        console.log(`[req] ${req.method} ${id} ${sub.slice(0, 140)}`);
      }

      // Connect-RPC workspace service (POST /workspace/{Service}/{Method}).
      const ws = sub.match(/^\/workspace\/([^/]+)\/([^/]+)$/);
      if (ws) {
        const body = await readBody(req);
        return workspaceMethod(res, ws[2], Buffer.from(body, "utf8"));
      }

      if (sub === "" && req.method === "GET") {
        const s = sessionById.get(id);
        if (!s) return sendJson(res, 404, { error: { code: "not_found", message: "no such session" } });
        return sendJson(res, 200, sessionSnapshot(s));
      }

      if (sub === "/items" && req.method === "GET") {
        const items = itemsBySession.get(id);
        if (!items) return sendJson(res, 404, { error: { code: "not_found", message: "no such session" } });
        return sendJson(res, 200, sliceItems(items, url));
      }

      if (sub === "/stream" && req.method === "GET") {
        if (!sessionById.has(id)) return sendJson(res, 404, { error: { code: "not_found", message: "no such session" } });
        return openSse(res, id);
      }

      // Mutations the app may fire (send/interrupt/rename/…) — accepted no-ops.
      if ((sub === "/events" || sub === "/interrupt" || sub === "/stop") && req.method === "POST") {
        return sendJson(res, 200, { ok: true, object: "response", id: "resp_noop", status: "completed" });
      }
      if (sub === "/rename" || sub === "/archive" || sub === "/unarchive" || sub === "/pin") {
        return sendJson(res, 200, { ok: true });
      }

      // Terminal / resource endpoints: empty lists.
      if (sub === "/resources/terminals") return sendJson(res, 200, { terminals: [] });
      if (sub === "/resources/environments/default") {
        return sendJson(res, 200, {
          metadata: {
            root: "/home/dev/example-project",
            home: "/home/dev",
            reachable: "WORKSPACE_REACH_REACHABLE",
          },
        });
      }
      if (sub === "/resources/environments/default/filesystem" || sub.startsWith("/resources/environments/default/filesystem/")) {
        const rawPath = sub === "/resources/environments/default/filesystem"
          ? ""
          : decodeURIComponent(sub.slice("/resources/environments/default/filesystem/".length));
        const knownFile = tree.find((e) => e.type === "ENTRY_TYPE_FILE" && e.path === rawPath);
        if (knownFile) {
          const content = fileContentFor(knownFile.path);
          return sendJson(res, 200, {
            object: "session.environment.filesystem.file_content",
            path: knownFile.path,
            content_type: "text/plain",
            encoding: "utf-8",
            content,
            bytes: content.length,
            truncated: false,
          });
        }
        const depth = rawPath ? rawPath.split("/").length : 0;
        const children = [];
        for (const e of tree) {
          if (!e.path.startsWith(rawPath ? rawPath + "/" : "")) continue;
          const segs = e.path.split("/");
          if (segs.length === depth + 1) {
            children.push({
              id: e.path,
              name: segs[segs.length - 1],
              path: e.path,
              type: e.type === "ENTRY_TYPE_DIRECTORY" ? "directory" : "file",
              bytes: e.type === "ENTRY_TYPE_FILE" ? (e.size ?? 0) : null,
              modified_at: NOW - 60,
            });
          }
        }
        return sendJson(res, 200, { data: children, has_more: false, truncated: false });
      }
      if (sub === "/resources/environments/default/search" || sub.startsWith("/resources/environments/default/search/")) {
        const q = (url.searchParams.get("q") ?? "").toLowerCase();
        const results = tree
          .filter((e) => e.type === "ENTRY_TYPE_FILE" && e.path.toLowerCase().includes(q))
          .slice(0, Number(url.searchParams.get("limit") ?? 50))
          .map((e) => ({
            id: e.path,
            name: e.name,
            path: e.path,
            type: "file",
            bytes: e.size ?? 0,
            modified_at: NOW - 60,
          }));
        return sendJson(res, 200, { data: results, has_more: false, truncated: false });
      }
      if (sub === "/resources/environments/default/changes") {
        return sendJson(res, 200, {
          object: "list",
          data: changes.map((c) => {
            const name = c.path.split("/").at(-1);
            const status = c.change_type === "CHANGE_TYPE_ADDED" ? "created" : c.change_type === "CHANGE_TYPE_DELETED" ? "deleted" : "modified";
            return {
              path: c.path,
              name,
              status,
              bytes: 2400,
              modified_at: NOW - 60,
              lines_added: Number(c.additions ?? 0),
              lines_removed: Number(c.deletions ?? 0),
            };
          }),
          has_more: false,
        });
      }
      if (sub?.startsWith("/resources/environments/default/diff/")) {
        const filePath = decodeURIComponent(sub.slice("/resources/environments/default/diff/".length));
        const content = fileContentFor(filePath);
        return sendJson(res, 200, {
          object: "session.environment.filesystem.file_diff",
          path: filePath,
          before: `// original ${filePath}\nconst value = 1;\n${content.slice(0, 2000)}`,
          after: content,
          requestedAt: Date.now(),
        });
      }
      if (sub?.startsWith("/resources/github")) {
        return sendJson(res, 404, { error: { code: "not_found", message: "Resource 'github' not found" } });
      }
      if (sub === "/child_sessions") {
        return sendJson(res, 200, { object: "list", data: [], has_more: false });
      }
      if (sub === "/agent") {
        return sendJson(res, 200, {
          id: "ag_perf",
          name: "example-agent",
          description: "Mocked agent for the performance baseline",
          harness: "example",
        });
      }
      if (sub === "/owner") return sendJson(res, 200, { owner: "dev@example.com" });
      if (sub === "/side_chats") return sendJson(res, 200, { side_chats: [], has_more: false });
      if (sub === "/read-state" && req.method === "PUT") return sendJson(res, 200, { ok: true });
      if (sub === "/journal") return sendJson(res, 200, { entries: [], next_cursor: null });
      if (sub === "/tasks") return sendJson(res, 200, { tasks: [] });
      if (sub === "/comments") return sendJson(res, 200, { comments: [], has_more: false });
      if (sub === "/usage") return sendJson(res, 200, { usage_by_model: {}, context_tokens: 42000, context_window: 200000 });
      if (sub === "/pull_requests") return sendJson(res, 200, { pull_requests: [] });
      if (sub === "/rewind/prompts") return sendJson(res, 200, { prompts: [] });
      if (sub?.startsWith("/permissions")) return sendJson(res, 200, { permission_level: null, rules: [] });
    }

    counters.unmatched++;
    logUnmatched(req.method, req.url);
    return sendJson(res, 404, { error: { code: "not_found", message: `mock: no handler for ${p}` } });
  } catch (err) {
    counters.unmatched++;
    logUnmatched(`ERROR ${req.method}`, `${req.url} ${err?.stack ?? err}`);
    return sendJson(res, 500, { error: { code: "internal", message: String(err) } });
  }
});

// WebSocket upgrade for the session-updates stream.
server.on("upgrade", (req, socket) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/v1/sessions/updates") {
    wsAttach(req, socket, (str) => socket.write(wsFrame(str)));
    return;
  }
  // Terminal-attach sockets are opened on demand; accept and stay silent.
  if (url.pathname.startsWith("/v1/sessions/") && url.pathname.endsWith("/terminal")) {
    const key = req.headers["sec-websocket-key"];
    if (key) {
      socket.write(
        "HTTP/1.1 101 Switching Protocols\r\n" +
          "Upgrade: websocket\r\n" +
          "Connection: Upgrade\r\n" +
          `Sec-WebSocket-Accept: ${wsAccept(key)}\r\n\r\n`,
      );
      const hb = setInterval(() => socket.write(wsFrame("{}")), 20000);
      socket.on("close", () => clearInterval(hb));
      socket.on("error", () => clearInterval(hb));
      return;
    }
  }
  socket.destroy();
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[perf-mock] serving ${DIST} on http://127.0.0.1:${server.address().port}`);
  console.log(`[perf-mock] unmatched-request log: ${LOG_PATH}`);
});
