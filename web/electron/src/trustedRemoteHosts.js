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
    const serverUrl = trustedRemoteTarget(entry.server_url);
    const hostId = normalizeTrustedHostId(entry.host_id);
    if (serverUrl === null || hostId === null) continue;
    if (hosts.some((host) => host.serverUrl === serverUrl && host.hostId === hostId)) continue;
    hosts.push({ serverUrl, hostId });
  }
  return hosts;
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
  isTrustedRemoteContext,
};
