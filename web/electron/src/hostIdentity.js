"use strict";

const { performance } = require("node:perf_hooks");
const { normalizeServerTarget } = require("./url");

const SESSION_HOST_PROTOCOL = 1;

/** Return whether the host is this machine and its live daemon serves the server.
 * Records without host_id are accepted; host-ID equality routes, enrollment authenticates. */
function isHostThisMachine({ hostId, serverUrl }, cli = require("./omnigent_cli")) {
  if (!hostId || hostId !== cli.readLocalHostId()) return false;
  const record = cli.liveDaemonRecordFor(serverUrl);
  if (record === null || record === undefined) return false;
  const recordHostId = record.host_id?.replace(/^host_/, "");
  return !recordHostId || recordHostId === hostId;
}

/** Cache local identity/enrollment checks for at most one second per host/server pair. */
function createHostThisMachineChecker({ cli, now = () => performance.now() } = {}) {
  const snapshots = new Map();
  return ({ hostId, serverUrl }) => {
    if (!hostId) return false;
    const timestamp = now();
    const key = JSON.stringify([hostId, serverUrl]);
    const cached = snapshots.get(key);
    if (cached && timestamp < cached.expiresAt) return cached.eligible;
    for (const [cachedKey, snapshot] of snapshots) {
      if (timestamp >= snapshot.expiresAt) snapshots.delete(cachedKey);
    }
    const eligible = isHostThisMachine({ hostId, serverUrl }, cli);
    snapshots.set(key, { eligible, expiresAt: timestamp + 1000 });
    return eligible;
  };
}

const IDENTITY_START = "__OMNIGENT_HOST_IDENTITY_START__";
const IDENTITY_END = "__OMNIGENT_HOST_IDENTITY_END__";

/** Read the marked daemon status produced by a remote host connection. */
function readRemoteHostIdentity(output, serverUrl, onUnavailable = () => {}) {
  const unavailable = (reason) => {
    onUnavailable(reason);
    return null;
  };
  const target = normalizeServerTarget(serverUrl);
  const start = output.lastIndexOf(IDENTITY_START);
  const end = output.indexOf(IDENTITY_END, start);
  if (!target) return unavailable("invalid selected target");
  if (start < 0 || end < 0) return unavailable("status markers missing");
  try {
    const body = JSON.parse(output.slice(start + IDENTITY_START.length, end));
    if (!Array.isArray(body.daemons) || body.daemons.length !== 1) {
      return unavailable("status must contain exactly one daemon");
    }
    const daemon = body.daemons[0];
    // CLI status normalizes the UI mount to the API mount and drops ?o=.
    // The selected workspace remains bound by the command's --server argument.
    const expected = new URL(target);
    const requestedOrg = expected.searchParams.get("o");
    expected.search = "";
    // A daemon target may carry ?o=: the requested one, or the one sign-in
    // resolved when none was requested. Another workspace's never matches.
    const daemonTarget = (() => {
      const raw = normalizeServerTarget(daemon.target);
      if (!raw) return null;
      const url = new URL(raw);
      const org = url.searchParams.get("o");
      if (org !== null && requestedOrg !== null && org !== requestedOrg) return null;
      url.search = "";
      return url.href;
    })();
    // The CLI reuses a daemon on another URL for the same workspace and names
    // the requested target in same_workspace_as.
    const servesTarget =
      (daemonTarget === expected.href &&
        normalizeServerTarget(daemon.server_url) === expected.href) ||
      (typeof daemon.same_workspace_as === "string" &&
        normalizeServerTarget(daemon.same_workspace_as) === expected.href);
    if (
      daemon.mode !== "server" ||
      daemon.process !== "online" ||
      daemon.host_status !== "online" ||
      daemon.error != null ||
      !servesTarget ||
      typeof daemon.host_id !== "string" ||
      !/^[a-f0-9]{32}$/i.test(daemon.host_id)
    ) {
      return unavailable("daemon status or target not eligible");
    }
    return { serverUrl: target, hostId: daemon.host_id.toLowerCase() };
  } catch {
    return unavailable("status could not be parsed");
  }
}

module.exports = {
  createHostThisMachineChecker,
  isHostThisMachine,
  SESSION_HOST_PROTOCOL,
  readRemoteHostIdentity,
  IDENTITY_START,
  IDENTITY_END,
};
