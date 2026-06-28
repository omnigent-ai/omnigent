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

const cli = require("./omnigent_cli");

/** Max seconds to wait for `host` to print its connected marker before giving up. */
const CONNECT_TIMEOUT_MS = 30000;
/** Grace period after SIGTERM before escalating to SIGKILL on shutdown. */
const KILL_GRACE_MS = 4000;
/** The line `omnigent host` prints once the websocket tunnel is up. */
const CONNECTED_MARKER = "✓ Connected";
/** Cap the in-memory per-host log so a chatty daemon can't grow unbounded. */
const MAX_LOG_CHARS = 8000;

/** serverUrl(normalized) -> { child, serverUrl, log } for host processes we started. */
const hostChildren = new Map();

/** serverUrl(normalized) -> in-flight ensureHostConnected promise (dedup). */
const connectingHosts = new Map();

/**
 * Every `omnigent host` child we have spawned and not yet seen exit — including
 * one still mid-connect, before it lands in `hostChildren` (the connect await
 * can take up to CONNECT_TIMEOUT_MS). `shutdown` SIGTERMs this set so a quit
 * during a connect can't orphan the child. Entries self-remove on exit.
 */
const spawnedHostChildren = new Set();

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

/**
 * Heuristically classify a host-connect error as an authentication failure,
 * from `omnigent host`'s own messages (HostConnectError: "Authentication
 * failed", "HTTP 401", login-page redirect, or the `omnigent login` hint). Lets
 * the UI show a friendly "sign in" prompt instead of a scary raw error.
 *
 * @param {string | undefined} text
 * @returns {boolean}
 */
function isAuthError(text) {
  return /authentication failed|http 401|unauthor|login page|omnigent login/i.test(
    String(text || ""),
  );
}

/**
 * True when `omnigent host` refused to start because a daemon already serves
 * this target — which means a host is in fact already connected, so we can
 * adopt it instead of treating the conflict as a failure.
 *
 * @param {string | undefined} text
 * @returns {boolean}
 */
function isDaemonConflict(text) {
  return /already running for this server|host daemon is already running/i.test(String(text || ""));
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
    // Track from the instant it exists so `shutdown` can kill it even while the
    // connect is still in flight (not yet in hostChildren). Self-removes on exit.
    spawnedHostChildren.add(child);
    child.once("exit", () => spawnedHostChildren.delete(child));

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
  // Ping the renderer right away so it re-reads (e.g. refetches the server's
  // host list), then again once the connect settles.
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
  // Adopt only a daemon we can VERIFY is connected (live process + an online
  // tunnel, via the fast disk-read + single HTTP probe — not the slow `omnigent
  // host status` subprocess). PID-liveness alone is not enough: a stale registry
  // record whose pid was recycled by an unrelated process would otherwise make
  // us "adopt" a daemon that isn't there. If a genuine daemon exists but its
  // tunnel is momentarily down, we fall through to spawn and the isDaemonConflict
  // backstop below adopts it instead of creating a duplicate.
  const conn = await cli.getHostConnectionFast(serverUrl);
  if (conn.connected) {
    return { ok: true, ownedByDesktop: false, adopted: true };
  }

  const spawned = await spawnHostChild(cliPath, serverUrl);
  if (!spawned.ok) {
    // Connect failed or timed out. Await the child's termination — escalating to
    // SIGKILL after the grace period — rather than firing a single SIGTERM and
    // moving on: a child that ignores or is slow to handle SIGTERM would keep
    // running as a connected host while we report {ok:false}, leaving the
    // machine hosting in contradiction to the state we return. stopChild is a
    // no-op for a child that already exited (the common conflict/error case).
    await stopChild(spawned.child);
    // The CLI refuses to start a second daemon for a target already served by
    // one (e.g. a local-mode daemon our pre-check couldn't match). That means a
    // host is in fact already connected — adopt it rather than report failure.
    if (isDaemonConflict(spawned.error)) {
      return { ok: true, ownedByDesktop: false, adopted: true };
    }
    return {
      ok: false,
      ownedByDesktop: false,
      error: spawned.error,
      authError: isAuthError(spawned.error),
    };
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
 * Ensure the CLI is authenticated for a server before connecting a host to it.
 * Local (loopback) servers need no auth. For a remote server with no valid
 * stored credentials, runs `omnigent login <url>` (browser/OIDC/Databricks; a
 * no-op when the server needs no auth). Returns ok when already authed, after a
 * successful login, or for a no-auth server; an error (pointing at `omnigent
 * login`) when login fails — e.g. a password/TTY mode that can't run headless.
 *
 * @param {string} cliPath
 * @param {string} serverUrl
 * @returns {Promise<{ ok: boolean, error?: string }>}
 */
async function ensureServerAuth(cliPath, serverUrl) {
  if (cli.isLoopbackServer(serverUrl) || cli.serverAuthed(serverUrl)) return { ok: true };
  const res = await cli.loginServer(cliPath, serverUrl);
  if (res.ok) return { ok: true };
  return {
    ok: false,
    error: `Sign-in required — run \`omnigent login ${serverUrl}\` in a terminal, then try again.`,
  };
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
  // Reuse a server that's already running — but health-verify it (pidfile +
  // pid + /health), not just pid-liveness, since we're about to navigate the
  // window to this URL: a stale pidfile (dead/reused pid, hung server) must NOT
  // be reused or we'd send the window to a dead URL. Still far faster than
  // `omnigent server status` (a Python cold start). We didn't start it, so no
  // ownership claim.
  const existing = await cli.localServerHealthy();
  if (existing) {
    return { ok: true, url: existing.url, alreadyRunning: true };
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
 * Tear down everything this app started: SIGTERM all host children (await their
 * exit within the grace period), then stop an owned local server. Called from
 * the app's before-quit handler.
 *
 * @param {string | null} cliPath
 * @returns {Promise<void>}
 */
async function shutdown(cliPath) {
  // Iterate the spawned-children set, not hostChildren: it also covers a child
  // still mid-connect (spawned but not yet tracked in hostChildren), so a quit
  // during a connect can't leave an orphaned `omnigent host` process.
  const exits = [];
  for (const child of spawnedHostChildren) {
    exits.push(stopChild(child));
  }
  await Promise.all(exits);
  hostChildren.clear();
  spawnedHostChildren.clear();
  if (cliPath) await stopOwnedLocalServer(cliPath);
}

module.exports = {
  ensureHostConnected,
  ensureServerAuth,
  disconnectHost,
  restartHost,
  startLocalServer,
  stopOwnedLocalServer,
  shutdown,
  onChange,
  // Exposed for tests / introspection.
  _hostChildren: hostChildren,
  ownsLiveHost,
};
