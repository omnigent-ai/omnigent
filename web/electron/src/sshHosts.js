"use strict";

// Which SSH destination reaches each host this desktop connected with "Connect
// over SSH", per server. Only main writes it; pages and servers never name one.

const fs = require("node:fs");
const path = require("node:path");
const { validateDestination } = require("./ssh_connect");

// Recorded ids are dashless lowercase UUIDs; servers may send the dashed form.
const canonicalHostId = (rawHostId) => String(rawHostId).replace(/-/g, "").toLowerCase();

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
      const key = canonicalHostId(hostId);
      const next = { ...current, [serverTarget]: { ...current[serverTarget], [key]: destination } };
      const file = filePath();
      fs.mkdirSync(path.dirname(file), { recursive: true });
      // Write a new file and rename it over the old one, so a crash can't truncate the record.
      const temp = `${file}.${process.pid}.tmp`;
      try {
        fs.writeFileSync(temp, JSON.stringify(next, null, 2), { mode: 0o600 });
        fs.renameSync(temp, file);
      } catch (error) {
        fs.rmSync(temp, { force: true });
        throw error;
      }
      // Cached only once on disk, so a failed write isn't served until restart.
      hosts = next;
    },

    /** The recorded destination for `hostId` on `serverTarget`, or null. */
    destination(serverTarget, rawHostId) {
      const hostId = canonicalHostId(rawHostId);
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
