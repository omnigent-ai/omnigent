"use strict";

// Localhost previews for sessions whose host this desktop already reaches over
// the user's own SSH. Only hosts with a destination main recorded after proving
// it (see sshHosts) qualify, and only once the user allows them in this run.

const { canonicalHostId } = require("./sshHosts");

/**
 * @param {{ destination: (serverUrl: string, hostId: string) => string | null }} deps
 *   The recorded SSH destination for a host on a server, or null.
 */
function createSshPreviewHosts({ destination }) {
  // Allowed hosts, each with the destination the user saw when allowing it.
  const allowed = new Map();
  // "Not now" holds until restart, so a retrying agent can't keep asking.
  const declined = new Set();
  const pending = new Map();

  const keyFor = (serverUrl, hostId) => JSON.stringify([serverUrl, canonicalHostId(hostId)]);

  function recorded(serverUrl, hostId) {
    if (typeof serverUrl !== "string" || typeof hostId !== "string" || !hostId) return null;
    const found = destination(serverUrl, hostId);
    return typeof found === "string" && found ? found : null;
  }

  return {
    /**
     * Whether `hostId`'s sessions may open localhost pages. A host whose recorded
     * destination changed since the user allowed it no longer qualifies.
     *
     * @param {string | null | undefined} serverUrl
     * @param {string | null | undefined} hostId
     * @returns {boolean}
     */
    allows(serverUrl, hostId) {
      const current = recorded(serverUrl, hostId);
      return current !== null && allowed.get(keyFor(serverUrl, hostId)) === current;
    },

    /**
     * Ask once whether `hostId`'s sessions may open localhost pages. Concurrent
     * requests share one question; hosts without a recorded destination are never asked.
     *
     * @param {string | null | undefined} serverUrl
     * @param {string | null | undefined} hostId
     * @param {(destination: string) => Promise<boolean | null>} approve
     *   Shows the question; null when it couldn't be shown.
     * @returns {Promise<boolean>} Whether the host is allowed now.
     */
    request(serverUrl, hostId, approve) {
      const asked = recorded(serverUrl, hostId);
      if (asked === null) return Promise.resolve(false);
      const key = keyFor(serverUrl, hostId);
      if (allowed.get(key) === asked) return Promise.resolve(true);
      if (declined.has(key)) return Promise.resolve(false);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight;
      const attempt = Promise.resolve()
        .then(() => approve(asked))
        .catch(() => null)
        .then((answer) => {
          if (answer === false) declined.add(key);
          // The record can change while the question is open; the answer covers only what was shown.
          if (answer !== true || recorded(serverUrl, hostId) !== asked) return false;
          allowed.set(key, asked);
          return true;
        })
        .finally(() => pending.delete(key));
      pending.set(key, attempt);
      return attempt;
    },

    /**
     * Why a recorded host's localhost pages stay blocked, or null.
     *
     * @param {string | null | undefined} serverUrl
     * @param {string | null | undefined} hostId
     * @returns {string | null}
     */
    hint(serverUrl, hostId) {
      const current = recorded(serverUrl, hostId);
      if (current === null || !declined.has(keyFor(serverUrl, hostId))) return null;
      return `Localhost previews from "${current}" were declined; restart Omnigent to be asked again.`;
    },
  };
}

module.exports = { createSshPreviewHosts };
