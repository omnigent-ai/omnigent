// Process lifecycle for desktop-managed Omnigent servers and host connections.
//
// This is the only place the desktop spawns long-lived processes. It owns:
//   - hostChildren: the foreground `omnigent host --server <url>` processes this
//     app started. They are torn down when the app quits (the confirmed
//     lifecycle: the desktop owns what it starts).
//   - ownedLocalServer: a local `omnigent server` we started ourselves (and so
//     are responsible for stopping). If a server was already running when we
//     looked, we do NOT claim ownership and leave it alone.
//
// Status is never cached here — every query re-reads it from the CLI
// (omnigent_cli.js), which is the single source of truth. This module only
// tracks *ownership* (did we start it?), which the CLI can't tell us.

"use strict";

const { spawn } = require("child_process");
const net = require("net");

const cli = require("./omnigent_cli");

/** Max seconds to wait for `host` to print its connected marker before giving up. */
const CONNECT_TIMEOUT_MS = 30000;
/** Grace period after SIGTERM before escalating to SIGKILL on shutdown. */
const KILL_GRACE_MS = 4000;
/** The line `omnigent host` prints once the websocket tunnel is up. */
const CONNECTED_MARKER = "✓ Connected";
/** Cap the in-memory per-host log so a chatty daemon can't grow unbounded. */
const MAX_LOG_CHARS = 8000;
/**
 * Max time to wait for a just-stopped local server's port to free up before
 * restarting. `omnigent server start` prefers the stable port (6767) but falls
 * back to a free one if the old port isn't rebindable yet, which would move the
 * server (and break the window pointed at it) — so we wait for the port first.
 */
const PORT_FREE_TIMEOUT_MS = 5000;

/** serverUrl(normalized) -> { child, serverUrl, log } for host processes we started. */
const hostChildren = new Map();

/** serverUrl(normalized) -> in-flight ensureHostConnected promise (dedup). */
const connectingHosts = new Map();

/** { url, port, pid } when this app started the local server; null otherwise. */
let ownedLocalServer = null;

/** Single listener notified when a host child's lifecycle changes (no polling). */
let changeListener = null;

/**
 * Register a callback fired when a managed host child connects or exits on its
 * own, so the main process can push a status ping to the renderer without
 * polling. One listener; a second call replaces the first.
 *
 * @param {(() => void) | null} cb
 */
function onChange(cb) {
  changeListener = typeof cb === "function" ? cb : null;
}

/** Fire the change listener, swallowing listener errors. */
function emitChange() {
  if (changeListener) {
    try {
      changeListener();
    } catch {
      // A broken listener must not take down lifecycle handling.
    }
  }
}

/**
 * Append to a capped log buffer (newest kept).
 *
 * @param {{ text: string }} holder
 * @param {string} chunk
 */
function appendLog(holder, chunk) {
  holder.text = (holder.text + chunk).slice(-MAX_LOG_CHARS);
}

/**
 * True when we hold a live (not yet exited) host child for this server.
 *
 * @param {string} key Normalized server URL.
 * @returns {boolean}
 */
function ownsLiveHost(key) {
  const entry = hostChildren.get(key);
  return Boolean(entry && entry.child.exitCode === null && !entry.child.killed);
}

/**
 * Spawn `omnigent host --server <url>` and resolve once it reports connected
 * (or fails / times out). On success the child keeps running; the caller
 * registers it. Never rejects.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, child: import("child_process").ChildProcess, holder: {text: string}, error?: string }>}
 */
function spawnHostChild(cliPath, serverUrl) {
  return new Promise((resolve) => {
    const holder = { text: "" };
    let child;
    try {
      child = spawn(cliPath, ["host", "--server", serverUrl], {
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      resolve({ ok: false, child: null, holder, error: err.message });
      return;
    }

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const timer = setTimeout(() => {
      finish({ ok: false, child, holder, error: "timed out waiting for host to connect" });
    }, CONNECT_TIMEOUT_MS);

    const onData = (buf) => {
      const text = buf.toString();
      appendLog(holder, text);
      if (text.includes(CONNECTED_MARKER)) finish({ ok: true, child, holder });
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (err) => finish({ ok: false, child, holder, error: err.message }));
    // An exit *before* the connected marker is a failure (auth error, conflict,
    // bad URL). The settled guard makes this a no-op once connected, so the
    // persistent cleanup listener (registered by the caller) handles later exits.
    child.on("exit", (code, signal) =>
      finish({
        ok: false,
        child,
        holder,
        error: holder.text.trim() || `host exited (code=${code}, signal=${signal})`,
      }),
    );
  });
}

/**
 * Ensure this machine is connected as a host to `serverUrl`.
 *
 * If a live daemon already serves it (e.g. one the user started by hand), we
 * *adopt* it without spawning a duplicate — `omnigent host` would otherwise
 * error on the conflict, and we must not kill a daemon we didn't start. Adopted
 * connections report ownedByDesktop:false.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, ownedByDesktop: boolean, adopted?: boolean, error?: string }>}
 */
async function ensureHostConnected(cliPath, serverUrl) {
  const key = cli.normalizeServerUrl(serverUrl);
  if (key === "") return { ok: false, ownedByDesktop: false, error: "missing server URL" };
  if (ownsLiveHost(key)) return { ok: true, ownedByDesktop: true };
  // Dedupe concurrent connects for the same server (the restore-on-load path
  // racing the connect-time path, or a double-clicked Start) so we never spawn
  // two `omnigent host` processes for one target.
  const inflight = connectingHosts.get(key);
  if (inflight) return inflight;
  const op = connectHost(cliPath, serverUrl, key);
  connectingHosts.set(key, op);
  // Surface the "connecting…" state right away (statusFor reports it while the
  // key is in connectingHosts), then again once it settles.
  emitChange();
  try {
    return await op;
  } finally {
    connectingHosts.delete(key);
    emitChange();
  }
}

/**
 * The actual connect: adopt a daemon already serving this target, else spawn
 * and track one. Serialized per target by ensureHostConnected.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @param {string} key Normalized server URL.
 * @returns {Promise<{ ok: boolean, ownedByDesktop: boolean, adopted?: boolean, error?: string }>}
 */
async function connectHost(cliPath, serverUrl, key) {
  const status = await cli.getHostStatus(cliPath, serverUrl);
  const conn = cli.connectionFromStatus(status, serverUrl);
  if (conn.process === "online") {
    // A daemon is already up for this target — adopt rather than spawn.
    return { ok: true, ownedByDesktop: false, adopted: true };
  }

  const spawned = await spawnHostChild(cliPath, serverUrl);
  if (!spawned.ok) {
    if (spawned.child && spawned.child.exitCode === null) spawned.child.kill("SIGTERM");
    return { ok: false, ownedByDesktop: false, error: spawned.error };
  }
  hostChildren.set(key, { child: spawned.child, serverUrl, log: spawned.holder });
  // Persistent cleanup: drop the entry when this child eventually exits. If the
  // entry is still ours here, this is a SPONTANEOUS exit (crash / external
  // kill), not a user-initiated disconnect (which removes the entry first), so
  // ping the UI — this is how a dying daemon is reflected without polling.
  spawned.child.on("exit", () => {
    if (hostChildren.get(key)?.child === spawned.child) {
      hostChildren.delete(key);
      emitChange();
    }
  });
  return { ok: true, ownedByDesktop: true };
}

/**
 * Disconnect this machine from `serverUrl`. A desktop-owned child is killed; a
 * daemon we merely adopted is asked to stop via the CLI (the user explicitly
 * toggled off, so honoring that is correct even for an adopted daemon).
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
async function disconnectHost(cliPath, serverUrl) {
  const key = cli.normalizeServerUrl(serverUrl);
  const entry = hostChildren.get(key);
  if (entry) {
    hostChildren.delete(key);
    // Await the exit so a follow-up restart spawns fresh rather than adopting
    // the daemon we're tearing down.
    await stopChild(entry.child);
    return { ok: true };
  }
  // No desktop-owned child: ask the CLI to stop a daemon we'd adopted.
  const res = await cli.stopHost(cliPath, serverUrl);
  return { ok: res.ok, error: res.ok ? undefined : res.output };
}

/**
 * Restart this machine's host connection: stop (awaiting the daemon down), then
 * reconnect.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, ownedByDesktop: boolean, error?: string }>}
 */
async function restartHost(cliPath, serverUrl) {
  await disconnectHost(cliPath, serverUrl);
  return ensureHostConnected(cliPath, serverUrl);
}

/**
 * SIGTERM a child, escalating to SIGKILL after a grace period, and resolve once
 * it has actually exited.
 *
 * @param {import("child_process").ChildProcess} child
 * @returns {Promise<void>}
 */
function stopChild(child) {
  return new Promise((resolve) => {
    if (!child || child.exitCode !== null) {
      resolve();
      return;
    }
    const t = setTimeout(() => {
      if (child.exitCode === null) child.kill("SIGKILL");
    }, KILL_GRACE_MS);
    // Don't let the escalation timer keep the event loop alive at quit.
    if (typeof t.unref === "function") t.unref();
    child.once("exit", () => {
      clearTimeout(t);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

/**
 * Start (or reuse) the local background server. Ownership is recorded only when
 * *we* actually start it — a server that was already running is left to its
 * own lifecycle.
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, url?: string, alreadyRunning?: boolean, error?: string }>}
 */
async function startLocalServer(cliPath) {
  const before = await cli.getServerStatus(cliPath);
  if (before && before.running && typeof before.url === "string") {
    return { ok: true, url: before.url, alreadyRunning: true };
  }
  const res = await cli.startLocalServer(cliPath);
  if (res.ok) {
    ownedLocalServer = { url: res.url, port: res.port, pid: res.pid };
    return { ok: true, url: res.url };
  }
  return { ok: false, error: res.error };
}

/**
 * Stop the local server only if this app started it (used at quit). A server
 * the desktop didn't start is left running.
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, skipped?: boolean }>}
 */
async function stopOwnedLocalServer(cliPath) {
  if (!ownedLocalServer) return { ok: true, skipped: true };
  const res = await cli.stopLocalServer(cliPath);
  ownedLocalServer = null;
  return { ok: res.ok };
}

/**
 * Stop the local server unconditionally — the user explicitly asked for it from
 * the sidebar control, so honor it even if the desktop didn't start it.
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
async function stopLocalServer(cliPath) {
  const res = await cli.stopLocalServer(cliPath);
  ownedLocalServer = null;
  return { ok: res.ok, error: res.ok ? undefined : res.output };
}

/**
 * Resolve once 127.0.0.1:port is bindable (i.e. free), or after `timeoutMs`.
 * Used between a local-server stop and start so the freed port is reusable
 * before `omnigent server start` probes it.
 *
 * @param {number} port
 * @param {number} timeoutMs
 * @returns {Promise<boolean>} true once free, false on timeout.
 */
function waitForPortFree(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const attempt = () => {
      const tester = net.createServer();
      tester.once("error", () => {
        tester.close();
        if (Date.now() >= deadline) {
          resolve(false);
          return;
        }
        const t = setTimeout(attempt, 150);
        if (typeof t.unref === "function") t.unref();
      });
      tester.once("listening", () => {
        // Listening succeeded → the port is free. Closing a never-accepted
        // listen socket leaves no TIME_WAIT, so `server start` can rebind it.
        tester.close(() => resolve(true));
      });
      tester.listen(port, "127.0.0.1");
    };
    attempt();
  });
}

/**
 * Restart the local server, keeping it on the same port. We stop it, wait for
 * its port to actually free (so `omnigent server start`'s preferred-port probe
 * rebinds it instead of falling back to a new free port — which would move the
 * URL out from under the connected window), then start.
 *
 * @param {string} cliPath
 * @returns {Promise<{ ok: boolean, url?: string, error?: string }>}
 */
async function restartLocalServer(cliPath) {
  const before = cli.localServerStatus();
  await stopLocalServer(cliPath);
  if (before && Number.isInteger(before.port)) {
    await waitForPortFree(before.port, PORT_FREE_TIMEOUT_MS);
  }
  return startLocalServer(cliPath);
}

/**
 * Host-connection status for one server, plus whether we own the connection.
 *
 * @param {string | null} cliPath
 * @param {string} serverUrl
 * @returns {Promise<Record<string, unknown>>}
 */
async function statusFor(cliPath, serverUrl) {
  if (!cliPath) {
    return {
      cliInstalled: false,
      connected: false,
      process: "offline",
      hostStatus: null,
      ownedByDesktop: false,
      error: null,
    };
  }
  const key = cli.normalizeServerUrl(serverUrl);
  // A connect is in flight for this server (auto-restore, connect-time, or a
  // Start that hasn't tunneled yet) — report "connecting" without a subprocess
  // so the sidebar shows it immediately. `process: "online"` + `connected:
  // false` is how the renderer renders the connecting state.
  if (connectingHosts.has(key) && !ownsLiveHost(key)) {
    return {
      cliInstalled: true,
      connected: false,
      process: "online",
      hostStatus: null,
      ownedByDesktop: true,
      error: null,
    };
  }
  // We own a live host child (saw its "✓ Connected") — report connected
  // instantly without shelling out to the CLI. This is the common case after a
  // page refresh, where the daemon is already up.
  if (ownsLiveHost(key)) {
    return {
      cliInstalled: true,
      connected: true,
      process: "online",
      hostStatus: "online",
      ownedByDesktop: true,
      error: null,
    };
  }
  const status = await cli.getHostStatus(cliPath, serverUrl);
  const conn = cli.connectionFromStatus(status, serverUrl);
  return {
    cliInstalled: true,
    ...conn,
    ownedByDesktop: ownsLiveHost(key),
  };
}

/**
 * Local-server status for a loopback server URL, plus ownership. Returns null
 * for non-loopback URLs (the local-server controls don't apply remotely).
 *
 * @param {string | null} cliPath
 * @param {string} serverUrl
 * @returns {Promise<Record<string, unknown> | null>}
 */
async function serverStatusFor(cliPath, serverUrl) {
  if (!cliPath || !cli.isLoopbackServer(serverUrl)) return null;
  // Read the local-server pidfile (~/.omnigent/local_server.pid) directly — pid
  // + port, then a pid-liveness check. Instant and always fresh: no `omnigent
  // server status` subprocess, so the row appears immediately (incl. on a page
  // refresh). Only surface controls when that server is the one THIS window is
  // connected to (matching loopback port), not an unrelated background server.
  const local = cli.localServerStatus();
  if (!local || !cli.sameLoopbackServer(local.url, serverUrl)) return null;
  return {
    running: true,
    url: local.url,
    pid: local.pid,
    ownedByDesktop: Boolean(ownedLocalServer),
  };
}

/**
 * Tear down everything this app started: SIGTERM all host children (await their
 * exit within the grace period), then stop an owned local server. Called from
 * the app's before-quit handler.
 *
 * @param {string | null} cliPath
 * @returns {Promise<void>}
 */
async function shutdown(cliPath) {
  const exits = [];
  for (const [, entry] of hostChildren) {
    exits.push(stopChild(entry.child));
  }
  await Promise.all(exits);
  hostChildren.clear();
  if (cliPath) await stopOwnedLocalServer(cliPath);
}

module.exports = {
  ensureHostConnected,
  disconnectHost,
  restartHost,
  startLocalServer,
  stopOwnedLocalServer,
  stopLocalServer,
  restartLocalServer,
  statusFor,
  serverStatusFor,
  shutdown,
  onChange,
  // Exposed for tests / introspection.
  _hostChildren: hostChildren,
  ownsLiveHost,
};
