"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { stripVTControlCharacters } = require("node:util");
const { normalizeSafeServerUrl, quoteRemoteServerUrl, resolveArcaPath } = require("./arca");
const { ARCA_PREVIEW_TIMEOUT_MS } = require("./arcaPreviewConfig");
const { WORKSPACE_UI_PATH } = require("./url");

const OUTPUT_LIMIT = 8_192;
const SYSTEM_SSH_PATH = "/usr/bin/ssh";
const CONTROL_DESTINATION = "arca-preview.invalid";
const WORKSPACE_API_PATHS = new Set(["/api/2.0/omnigent", "/api/2.0/omnigents"]);

function muxControlArgs(socketPath, operation, forwardSpec) {
  const args = [
    "-F",
    "/dev/null",
    "-o",
    "ProxyCommand=/usr/bin/false",
    "-o",
    "BatchMode=yes",
    "-o",
    "ControlMaster=no",
    "-S",
    socketPath,
    "-O",
    operation,
  ];
  if (forwardSpec) args.push("-o", "ExitOnForwardFailure=yes", "-L", forwardSpec);
  args.push(CONTROL_DESTINATION);
  return args;
}

function loopbackPreview(url) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    if (!["localhost", "127.0.0.1"].includes(parsed.hostname)) return null;
    const port = Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80));
    if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
    return { origin: parsed.origin, host: parsed.hostname, port };
  } catch {
    return null;
  }
}

function serverIdentity(value) {
  try {
    const url = new URL(value);
    const pathname = url.pathname.replace(/\/+$/, "") || "/";
    const workspace = pathname === WORKSPACE_UI_PATH || WORKSPACE_API_PATHS.has(pathname);
    return {
      base: `${url.protocol}//${url.host}${workspace ? WORKSPACE_UI_PATH : pathname}`,
      workspace: url.searchParams.get("o"),
    };
  } catch {
    return null;
  }
}

function sameServer(left, right) {
  const a = serverIdentity(left);
  const b = serverIdentity(right);
  if (!a || !b || a.base !== b.base) return false;
  // The CLI persists the requested selector and routes this exact status probe
  // with its org header. The daemon record normally omits `?o=`, so its online
  // host_id attests the routed workspace; reject any selector it does include.
  return !a.workspace || (!!b.workspace && a.workspace === b.workspace);
}

function parseStatusJson(stdout) {
  for (let index = stdout.indexOf("{"); index >= 0; index = stdout.indexOf("{", index + 1)) {
    try {
      return JSON.parse(stdout.slice(index));
    } catch {
      // Arca may print startup notices before the JSON payload.
    }
  }
  return null;
}

function safeCommandDetail(value) {
  const printable = [...stripVTControlCharacters(String(value ?? ""))]
    .filter((character) => {
      const code = character.codePointAt(0);
      return (
        code === 9 || code === 10 || code === 13 || (code >= 32 && !(code >= 127 && code <= 159))
      );
    })
    .join("");
  return printable.trim().slice(-OUTPUT_LIMIT);
}

function terminate(child) {
  if (!child) return;
  const signal = (name) => {
    try {
      if (child.pid && child.spawnargs) process.kill(-child.pid, name);
      else child.kill(name);
    } catch {
      /* already exited */
    }
  };
  signal("SIGTERM");
  const timer = setTimeout(() => signal("SIGKILL"), 1_000);
  timer.unref?.();
}

function terminateAndWait(child, graceMs) {
  if (!child || child.exitCode != null || child.signalCode != null) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener?.("exit", finish);
      child.removeListener?.("error", finish);
      resolve();
    };
    const signal = (name) => {
      try {
        if (child.pid && child.spawnargs) process.kill(-child.pid, name);
        else child.kill(name);
      } catch {
        finish();
      }
    };
    child.once?.("exit", finish);
    child.once?.("error", finish);
    signal("SIGTERM");
    if (settled) return;
    timer = setTimeout(() => {
      signal("SIGKILL");
      finish();
    }, graceMs);
  });
}

function waitForChildExit(child, timeoutMs) {
  if (!child || child.exitCode != null || child.signalCode != null) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (exited) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener?.("exit", onExit);
      child.removeListener?.("error", onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    child.once?.("exit", onExit);
    child.once?.("error", onExit);
    const timer = setTimeout(() => finish(false), timeoutMs);
  });
}

function run(file, args, { spawnFn, deadline, onChild }) {
  return new Promise((resolve, reject) => {
    let child;
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timer;
    const finish = (error, code = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve({ code, stdout, stderr });
    };
    try {
      child = spawnFn(file, args, { stdio: ["ignore", "pipe", "pipe"], detached: true });
      onChild(child, () => finish(new Error("preview was cancelled")));
    } catch (error) {
      reject(error);
      return;
    }
    child.stdout?.on("data", (chunk) => (stdout = (stdout + String(chunk)).slice(-OUTPUT_LIMIT)));
    child.stderr?.on("data", (chunk) => (stderr = (stderr + String(chunk)).slice(-OUTPUT_LIMIT)));
    child.on("error", (error) => finish(error));
    child.on("exit", (code) => finish(null, code));
    if (settled) return;
    timer = setTimeout(
      () => {
        if (settled) return;
        finish(new Error("timed out preparing the Arca localhost preview"));
        terminate(child);
      },
      Math.max(0, deadline - Date.now()),
    );
    timer.unref?.();
  });
}

async function verifyArcaHost({ arcaPath, serverUrl, hostId, spawnFn, deadline, onChild }) {
  const safeServerUrl = normalizeSafeServerUrl(serverUrl);
  const result = await run(
    arcaPath,
    [
      "ssh",
      "-o",
      "ClearAllForwardings=yes",
      "isaac",
      "omni",
      "host",
      "status",
      "--server",
      quoteRemoteServerUrl(safeServerUrl),
      "--json",
    ],
    { spawnFn, deadline, onChild },
  );
  if (result.code !== 0) {
    throw Object.assign(new Error("could not verify the Arca preview host"), {
      commandDetail: safeCommandDetail(result.stderr),
    });
  }
  const daemon = parseStatusJson(result.stdout)?.daemons?.find(
    (item) =>
      item?.host_id === hostId &&
      sameServer(item.server_url, safeServerUrl) &&
      item.process === "online" &&
      item.host_status === "online",
  );
  if (!daemon) throw new Error("the requesting session is not running on this server's Arca host");
}

function waitForSocket(socketPath, deadline, isSocketReady, isCurrent) {
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (!isCurrent()) return reject(new Error("preview was cancelled"));
      if (isSocketReady(socketPath)) return resolve();
      if (Date.now() >= deadline) return reject(new Error("timed out starting Arca SSH"));
      const timer = setTimeout(poll, 20);
      timer.unref?.();
    };
    poll();
  });
}

function createArcaPreviewManager({
  resolveArcaPathFn = resolveArcaPath,
  spawnFn = spawn,
  sshPath = SYSTEM_SSH_PATH,
  timeoutMs = ARCA_PREVIEW_TIMEOUT_MS,
  socketReady = fs.existsSync,
  unlinkSocket = (socketPath) => fs.rmSync(socketPath, { force: true }),
  removeSocketDir = (socketDir) => fs.rmSync(socketDir, { recursive: true, force: true }),
  shutdownTimeoutMs = 500,
  terminationGraceMs = 1_000,
  socketPathFn = () => {
    const socketDir = fs.mkdtempSync(path.join("/tmp", "oa-"));
    return { socketPath: path.join(socketDir, "s"), socketDir };
  },
  onExit = () => {},
  logError = (...args) => console.warn(...args),
} = {}) {
  const owned = new Map();
  const shuttingDown = new Set();
  const latestShutdown = new Map();
  let sequence = 0;

  function cleanupSocket(state) {
    try {
      if (state.socketPath) unlinkSocket(state.socketPath);
    } catch {
      /* already removed */
    }
    try {
      if (state.socketDir) removeSocketDir(state.socketDir);
    } catch {
      /* already removed */
    }
  }

  function shutdownState(conversationId, state) {
    if (state.shutdownPromise) return state.shutdownPromise;
    if (owned.get(conversationId) === state) owned.delete(conversationId);
    state.cancel?.();
    const shutdown = (async () => {
      let control = null;
      if (state.master && !state.masterExited && state.socketPath) {
        try {
          // Keep the socket reachable until the exact owned mux master has
          // acknowledged exit or the bounded fallback takes ownership.
          control = spawnFn(sshPath, muxControlArgs(state.socketPath, "exit"), {
            stdio: ["ignore", "ignore", "ignore"],
            detached: true,
          });
        } catch {
          /* fall through to process-group termination */
        }
      }
      if (control && !(await waitForChildExit(control, shutdownTimeoutMs))) {
        await terminateAndWait(control, terminationGraceMs);
      }
      const processes = [...new Set([state.child, state.master].filter(Boolean))];
      await Promise.all(processes.map((child) => terminateAndWait(child, terminationGraceMs)));
      cleanupSocket(state);
    })();
    state.shutdownPromise = shutdown;
    shuttingDown.add(shutdown);
    latestShutdown.set(conversationId, shutdown);
    shutdown.finally(() => {
      shuttingDown.delete(shutdown);
      if (latestShutdown.get(conversationId) === shutdown) latestShutdown.delete(conversationId);
    });
    return shutdown;
  }

  function release(conversationId, token) {
    const current = owned.get(conversationId);
    if (current && (!token || current.token === token)) {
      return shutdownState(conversationId, current);
    }
    return latestShutdown.get(conversationId) || null;
  }

  async function prepare({ conversationId, url, hostId, serverUrl, deadline: requestedDeadline }) {
    const preview = loopbackPreview(url);
    if (!preview) {
      await release(conversationId);
      return null;
    }
    if (typeof hostId !== "string" || !hostId)
      throw new Error("the requesting session's host is unknown");
    const arcaPath = resolveArcaPathFn();
    if (!arcaPath) throw new Error("the arca CLI was not found on this machine");
    const token = ++sequence;
    const deadline = Math.min(requestedDeadline ?? Infinity, Date.now() + timeoutMs);
    if (deadline <= Date.now()) throw new Error("timed out preparing the Arca localhost preview");
    const state = {
      token,
      arcaPath,
      child: null,
      master: null,
      masterExited: false,
      cancel: null,
      socketPath: null,
      socketDir: null,
      shutdownPromise: null,
    };
    const previous = owned.get(conversationId);
    owned.set(conversationId, state);
    if (previous) shutdownState(conversationId, previous);
    const onChild = (child, cancel) => {
      if (owned.get(conversationId)?.token !== token) {
        terminate(child);
        cancel();
        return;
      }
      state.child = child;
      state.cancel = cancel;
    };
    try {
      await Promise.allSettled(
        [...shuttingDown].filter((shutdown) => shutdown !== state.shutdownPromise),
      );
      if (owned.get(conversationId)?.token !== token) throw new Error("preview was superseded");
      try {
        await verifyArcaHost({ arcaPath, serverUrl, hostId, spawnFn, deadline, onChild });
      } catch (error) {
        if (error.commandDetail) logError("[arca preview] status failed:", error.commandDetail);
        throw error;
      }
      if (owned.get(conversationId)?.token !== token) throw new Error("preview was superseded");
      const socket = socketPathFn();
      const socketPath = typeof socket === "string" ? socket : socket.socketPath;
      state.socketPath = socketPath;
      state.socketDir = typeof socket === "string" ? null : socket.socketDir;
      // ControlPersist=no keeps config from daemonizing away from our process
      // group. A sudden Electron SIGKILL can still orphan this stopgap.
      const master = spawnFn(
        arcaPath,
        [
          "ssh",
          "-M",
          "-S",
          socketPath,
          "-o",
          "ClearAllForwardings=yes",
          "-o",
          "ControlPersist=no",
          "-N",
        ],
        { stdio: ["ignore", "ignore", "pipe"], detached: true },
      );
      master.stderr?.resume?.();
      state.child = master;
      state.master = master;
      let rejectMaster;
      let preparing = true;
      const masterExit = new Promise((_, reject) => {
        rejectMaster = reject;
        master.on("error", (error) => {
          state.masterExited = true;
          if (preparing) reject(error);
        });
        master.on("exit", (code) => {
          state.masterExited = true;
          if (preparing) reject(new Error(`Arca preview exited (${code ?? "unknown"})`));
        });
      });
      state.cancel = () => rejectMaster(new Error("preview was cancelled"));
      await Promise.race([
        waitForSocket(
          socketPath,
          deadline,
          socketReady,
          () => owned.get(conversationId)?.token === token && !state.masterExited,
        ),
        masterExit,
      ]);
      const bindHosts = preview.host === "localhost" ? ["127.0.0.1", "[::1]"] : [preview.host];
      for (const bindHost of bindHosts) {
        const spec = `${bindHost}:${preview.port}:${preview.host}:${preview.port}`;
        // Each exact family is acknowledged independently; either failure
        // tears down the master and therefore rolls back the other forward.
        // eslint-disable-next-line no-await-in-loop
        const acknowledged = await Promise.race([
          run(sshPath, muxControlArgs(socketPath, "forward", spec), {
            spawnFn,
            deadline,
            onChild: (child, cancel) => {
              state.cancel = () => {
                terminate(child);
                cancel();
              };
            },
          }),
          masterExit,
        ]);
        if (acknowledged.code !== 0) {
          const detail = safeCommandDetail(acknowledged.stderr);
          if (detail) logError(`[arca preview] ${bindHost} forward failed:`, detail);
          const family = bindHost.includes(":") ? "IPv6" : "IPv4";
          throw new Error(
            `could not open localhost preview port ${preview.port} on ${family} (${bindHost})`,
          );
        }
        if (owned.get(conversationId)?.token !== token) {
          throw new Error("preview was superseded");
        }
      }
      state.child = master;
      state.cancel = null;
      preparing = false;
      master.on("exit", () => {
        if (owned.get(conversationId)?.token !== token) return;
        owned.delete(conversationId);
        cleanupSocket(state);
        try {
          onExit(conversationId);
        } catch (error) {
          logError(
            "[arca preview] exit callback failed:",
            safeCommandDetail(error?.message ?? error),
          );
        }
      });
      return {
        origin: preview.origin,
        release: () => shutdownState(conversationId, state),
      };
    } catch (error) {
      await release(conversationId, token);
      throw error;
    }
  }

  async function shutdownAll() {
    for (const [conversationId, state] of [...owned]) shutdownState(conversationId, state);
    await Promise.allSettled([...shuttingDown]);
  }

  return { prepare, release: (conversationId) => release(conversationId), shutdownAll };
}

module.exports = {
  createArcaPreviewManager,
  loopbackPreview,
  parseStatusJson,
  safeCommandDetail,
  sameServer,
};
