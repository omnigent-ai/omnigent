"use strict";

const omnigentCli = require("./omnigent_cli");

const SESSION_HOST_PROTOCOL = 1;

/** Return whether the host is this machine and its live daemon serves the server.
 * Records without host_id are accepted; host-ID equality routes, enrollment authenticates. */
function isHostThisMachine({ hostId, serverUrl }, cli = omnigentCli) {
  if (!hostId || hostId !== cli.readLocalHostId()) return false;
  const record = cli.liveDaemonRecordFor(serverUrl);
  return record != null && (!record.host_id || record.host_id === hostId);
}

module.exports = { isHostThisMachine, SESSION_HOST_PROTOCOL };
