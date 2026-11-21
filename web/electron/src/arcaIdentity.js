"use strict";

const { normalizedServerTarget, daemonServesTarget } = require("./hostStatusIdentity");

const IDENTITY_START = "__OMNIGENT_ARCA_IDENTITY_START__";
const IDENTITY_END = "__OMNIGENT_ARCA_IDENTITY_END__";

/** Canonical selected target, preserving mount and workspace selector. */
function arcaTarget(raw) {
  return normalizedServerTarget(raw);
}

/** Read only the bounded, machine-readable status from the Arca connect run. */
function readArcaIdentity(output, serverUrl, onUnavailable = () => {}) {
  const unavailable = (reason) => {
    onUnavailable(reason);
    return null;
  };
  const target = arcaTarget(serverUrl);
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
    if (
      daemon.mode !== "server" ||
      daemon.process !== "online" ||
      daemon.host_status !== "online" ||
      daemon.error != null ||
      !daemonServesTarget(daemon, target) ||
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

/** Main-owned identity cache; new/failed/unrecognized runs revoke older identity. */
function createArcaIdentityStore() {
  const runs = new Map();
  return {
    begin(serverUrl) {
      const target = arcaTarget(serverUrl);
      const run = { identity: null };
      if (target) runs.set(target, run);
      return (result) => {
        if (target && runs.get(target) === run) {
          run.identity =
            result.ok && result.identity?.serverUrl === target ? result.identity : null;
        }
        return result;
      };
    },
    get(serverUrl) {
      return runs.get(arcaTarget(serverUrl))?.identity ?? null;
    },
  };
}

function isArcaAgentContext(identity, context, { enabled, managed, serverTarget }) {
  return (
    enabled === true &&
    managed === true &&
    identity != null &&
    context != null &&
    identity.serverUrl === arcaTarget(serverTarget) &&
    context.serverTarget === identity.serverUrl &&
    typeof context.sourceHostId === "string" &&
    context.sourceHostId === identity.hostId
  );
}

module.exports = {
  arcaTarget,
  readArcaIdentity,
  createArcaIdentityStore,
  isArcaAgentContext,
  IDENTITY_START,
  IDENTITY_END,
};
