"use strict";

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
function createHostThisMachineChecker({ cli, now = Date.now } = {}) {
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

module.exports = { createHostThisMachineChecker, isHostThisMachine, SESSION_HOST_PROTOCOL };
