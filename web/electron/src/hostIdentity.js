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

module.exports = { isHostThisMachine, SESSION_HOST_PROTOCOL };
