"use strict";

const { normalizeServerTarget } = require("./url");

const { readRemoteHostIdentity, IDENTITY_START, IDENTITY_END } = require("./hostIdentity");

/** Canonical selected target, preserving mount and workspace selector. */
function arcaTarget(raw) {
  return normalizeServerTarget(raw);
}

/** Read only the bounded, machine-readable status from the Arca connect run. */
function readArcaIdentity(output, serverUrl, onUnavailable = () => {}) {
  return readRemoteHostIdentity(output, serverUrl, onUnavailable);
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
