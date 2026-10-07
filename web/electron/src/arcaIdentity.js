"use strict";

const { normalizeSavedServerUrl, databricksWorkspaceUiUrl } = require("./url");

const IDENTITY_START = "__OMNIGENT_ARCA_IDENTITY_START__";
const IDENTITY_END = "__OMNIGENT_ARCA_IDENTITY_END__";

/** Canonical selected target, preserving mount and workspace selector. */
function arcaTarget(raw) {
  try {
    const saved = normalizeSavedServerUrl(raw);
    const url = new URL(databricksWorkspaceUiUrl(saved) ?? saved);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.hash = "";
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    url.searchParams.sort();
    return url.href;
  } catch {
    return null;
  }
}

/** Read only the bounded, machine-readable status from the Arca connect run. */
function readArcaIdentity(output, serverUrl) {
  const target = arcaTarget(serverUrl);
  const start = output.lastIndexOf(IDENTITY_START);
  const end = output.indexOf(IDENTITY_END, start);
  if (!target || start < 0 || end < 0) return null;
  try {
    const body = JSON.parse(output.slice(start + IDENTITY_START.length, end));
    if (!Array.isArray(body.daemons) || body.daemons.length !== 1) return null;
    const daemon = body.daemons[0];
    // CLI status normalizes the UI mount to the API mount and drops ?o=.
    // The selected workspace remains bound by the command's --server argument.
    const expected = new URL(target);
    expected.search = "";
    if (
      daemon.mode !== "server" ||
      daemon.process !== "online" ||
      daemon.host_status !== "online" ||
      daemon.error != null ||
      arcaTarget(daemon.target) !== expected.href ||
      arcaTarget(daemon.server_url) !== expected.href ||
      typeof daemon.host_id !== "string" ||
      !/^[a-f0-9]{32}$/i.test(daemon.host_id)
    ) {
      return null;
    }
    return { serverUrl: target, hostId: daemon.host_id.toLowerCase() };
  } catch {
    return null;
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
