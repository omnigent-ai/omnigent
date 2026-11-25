// Minimal CDP client over Node's native WebSocket.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import { setTimeout as delay } from "node:timers/promises";

export function forEachSequential(items, action) {
  return items.reduce((previous, item, index) => previous.then(() => action(item, index)), Promise.resolve());
}

export function armOrder(index) {
  return index % 2 === 0 ? ["a", "b"] : ["b", "a"];
}

export function findChrome(explicit) {
  const cache = path.join(os.homedir(), ".cache", "ms-playwright");
  const playwright = fs.existsSync(cache)
    ? fs.readdirSync(cache).filter((name) => /^chromium-\d+$/.test(name)).sort((a, b) => Number(b.slice(9)) - Number(a.slice(9)))
        .flatMap((name) => [path.join(cache, name, "chrome-linux", "chrome"), path.join(cache, name, "chrome-linux64", "chrome"), path.join(cache, name, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"), path.join(cache, name, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing")])
    : [];
  const onPath = (name) => (process.env.PATH || "").split(path.delimiter).map((dir) => path.join(dir, name));
  const chosen = explicit || process.env.CHROME;
  const candidates = chosen
    ? (chosen.includes(path.sep) ? [chosen] : onPath(chosen))
    : ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", ...["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"].flatMap(onPath), ...playwright];
  for (const candidate of candidates) {
    try { fs.accessSync(candidate, fs.constants.X_OK); return candidate; } catch { continue; }
  }
  throw new Error("Chrome not found; pass --chrome or set CHROME");
}

export async function ensurePortFree(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`invalid port: ${port}`);
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

export function cdpConnect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  const listeners = new Set();
  let defaultSession = null;

  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", () => resolve(), { once: true });
    ws.addEventListener("error", () => reject(new Error(`CDP WS error: ${wsUrl}`)), { once: true });
  });

  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (msg.id !== undefined) {
      const p = pending.get(msg.id);
      if (p) {
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(`[${p.method}] ${msg.error.message} (code ${msg.error.code})`));
        else p.resolve(msg.result);
      }
      return;
    }
    if (msg.method) {
      for (const fn of listeners) fn(msg.method, msg.params, msg.sessionId);
    }
  });

  ws.addEventListener("close", () => {
    for (const p of pending.values()) p.reject(new Error("CDP socket closed"));
    pending.clear();
  });

  function send(method, params = {}, sessionId) {
    const id = nextId++;
    const envelope = { id, method, params };
    const sid = sessionId ?? defaultSession;
    if (sid) envelope.sessionId = sid;
    if (process.env.CDP_LOG) process.stderr.write(`cdp>${method} sid=${sid ?? "browser"}\n`);
    let pendingEntry;
    const p = new Promise((resolve, reject) => {
      pendingEntry = { resolve, reject, method };
      pending.set(id, pendingEntry);
    });
    // A wedged debugger must not hang the whole benchmark run.
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        pendingEntry.reject(new Error(`CDP send timeout: ${method}`));
      }
    }, 60000);
    const origResolve = pendingEntry.resolve;
    pendingEntry.resolve = (v) => {
      clearTimeout(timer);
      origResolve(v);
    };
    pendingEntry.reject = ((origReject) => (e) => {
      clearTimeout(timer);
      origReject(e);
    })(pendingEntry.reject);
    ws.send(JSON.stringify(envelope));
    return p;
  }

  function close() {
    try {
      ws.close();
    } catch { return; }
  }

  return {
    ready,
    send,
    setDefaultSession: (sid) => {
      defaultSession = sid;
    },
    onEvent: (fn) => listeners.add(fn),
    offEvent: (fn) => listeners.delete(fn),
    close,
  };
}

export async function waitHttp(url, timeoutMs = 15000, start = Date.now()) {
  try {
    const res = await globalThis.fetch(url);
    if (res.ok) return res.json();
  } catch {
    // The local server may still be starting.
  }
  if (Date.now() - start > timeoutMs) throw new Error(`timeout waiting for ${url}`);
  await delay(200);
  return waitHttp(url, timeoutMs, start);
}
