"use strict";

// Which SSH destination reaches each host this desktop connected with "Connect
// over SSH", per server. Only main writes it; pages and servers never name one.

const fs = require("node:fs");
const path = require("node:path");
const { validateDestination } = require("./ssh_connect");

/**
 * @param {() => string} filePath Where the record lives, e.g. in `userData`.
 */
function createSshHostStore(filePath) {
  let hosts = null;

  function load() {
    if (hosts) return hosts;
    try {
      const data = JSON.parse(fs.readFileSync(filePath(), "utf8"));
      hosts = data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch {
      // Missing or corrupt: nothing recorded yet.
      hosts = {};
    }
    return hosts;
  }

  return {
    /**
     * @param {string} serverTarget The window's server URL.
     * @param {string} hostId
     * @param {string} destination
     */
    record(serverTarget, hostId, destination) {
      const current = load();
      current[serverTarget] = { ...current[serverTarget], [hostId]: destination };
      fs.mkdirSync(path.dirname(filePath()), { recursive: true });
      fs.writeFileSync(filePath(), JSON.stringify(current, null, 2), { mode: 0o600 });
    },

    /** The recorded destination for `hostId` on `serverTarget`, or null. */
    destination(serverTarget, rawHostId) {
      // Recorded ids are dashless lowercase UUIDs; servers may send the dashed form.
      const hostId = String(rawHostId).replace(/-/g, "").toLowerCase();
      const servers = load();
      const recorded = Object.hasOwn(servers, serverTarget) ? servers[serverTarget] : null;
      const destination = recorded && Object.hasOwn(recorded, hostId) ? recorded[hostId] : null;
      try {
        return typeof destination === "string" ? validateDestination(destination) : null;
      } catch {
        return null;
      }
    },
  };
}

module.exports = { createSshHostStore };
