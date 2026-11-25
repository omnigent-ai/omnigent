"use strict";

const { normalizedServerTarget: trustedRemoteTarget } = require("./hostStatusIdentity");

/** Normalize a session host ID for comparison; the `host_` prefix is not significant. */
function normalizeTrustedHostId(hostId) {
  if (typeof hostId !== "string") return null;
  const normalized = hostId.replace(/^host_/, "").toLowerCase();
  return normalized === "" ? null : normalized;
}

/**
 * Parse the `trusted_remote_hosts` settings value into canonical entries.
 * Each entry names a server and the exact host ID trusted on it; malformed
 * entries are dropped so one bad line cannot break the whole list.
 */
function readTrustedRemoteHosts(value) {
  if (!Array.isArray(value)) return [];
  const hosts = [];
  for (const entry of value) {
    if (entry == null || typeof entry !== "object") continue;
    // Accept both the settings shape and the canonical shape, so a list
    // already parsed by this module round-trips through it unchanged.
    const serverUrl = trustedRemoteTarget(entry.server_url ?? entry.serverUrl);
    const hostId = normalizeTrustedHostId(entry.host_id ?? entry.hostId);
    if (serverUrl === null || hostId === null) continue;
    if (hosts.some((host) => host.serverUrl === serverUrl && host.hostId === hostId)) continue;
    hosts.push({ serverUrl, hostId });
  }
  return hosts;
}

/** Render canonical entries in the settings-file shape. */
function serializeTrustedRemoteHosts(entries) {
  return readTrustedRemoteHosts(entries).map((entry) => ({
    server_url: entry.serverUrl,
    host_id: entry.hostId,
  }));
}

/**
 * Add one entry, canonicalising both halves and dropping malformed ones.
 * Returns the full canonical list and whether the entry was new.
 */
function addTrustedRemoteHost(value, serverUrl, hostId) {
  const entries = readTrustedRemoteHosts(value);
  const server = trustedRemoteTarget(serverUrl);
  const host = normalizeTrustedHostId(hostId);
  if (server === null || host === null)
    return { entries: serializeTrustedRemoteHosts(entries), added: false };
  if (entries.some((entry) => entry.serverUrl === server && entry.hostId === host)) {
    return { entries: serializeTrustedRemoteHosts(entries), added: false };
  }
  entries.push({ serverUrl: server, hostId: host });
  return { entries: serializeTrustedRemoteHosts(entries), added: true };
}

/** Remove one entry. Returns the full canonical list and whether it was present. */
function removeTrustedRemoteHost(value, serverUrl, hostId) {
  const entries = readTrustedRemoteHosts(value);
  const server = trustedRemoteTarget(serverUrl);
  const host = normalizeTrustedHostId(hostId);
  if (server === null || host === null)
    return { entries: serializeTrustedRemoteHosts(entries), removed: false };
  const kept = entries.filter((entry) => !(entry.serverUrl === server && entry.hostId === host));
  return { entries: serializeTrustedRemoteHosts(kept), removed: kept.length !== entries.length };
}

/** Match explicit trust against both server targets and the session host. */
function isTrustedRemoteContext(entries, context, serverTarget) {
  if (!Array.isArray(entries) || context == null) return false;
  const target = trustedRemoteTarget(serverTarget);
  const source = trustedRemoteTarget(context.serverTarget);
  const hostId = normalizeTrustedHostId(context.sourceHostId);
  if (target === null || source === null || hostId === null) return false;
  return entries.some(
    (entry) => entry.serverUrl === target && entry.serverUrl === source && entry.hostId === hostId,
  );
}

module.exports = {
  trustedRemoteTarget,
  normalizeTrustedHostId,
  readTrustedRemoteHosts,
  serializeTrustedRemoteHosts,
  addTrustedRemoteHost,
  removeTrustedRemoteHost,
  isTrustedRemoteContext,
};
