"use strict";

const omnigentCli = require("./omnigent_cli");

/** Only servers this machine's live daemon is enrolled with can grant this;
 * host-ID equality picks the session, it doesn't authenticate it. */
function isHostThisMachine({ hostId, serverUrl }, cli = omnigentCli) {
  if (!hostId || hostId !== cli.readLocalHostId()) return false;
  const record = cli.liveDaemonRecordFor(serverUrl);
  return record != null && (!record.host_id || record.host_id === hostId);
}

module.exports = { isHostThisMachine };
