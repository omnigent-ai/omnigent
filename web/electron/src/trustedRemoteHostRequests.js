"use strict";

// Trusting a remote host is an explicit, persistent grant: one native dialog
// per host until the user answers, and a decline holds until restart so a
// retrying agent cannot keep asking. The grant itself lives in settings.

const { normalizeTrustedHostId, trustedRemoteTarget } = require("./trustedRemoteHosts");

const TRUSTED_HOST_INPUT_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/**
 * @param {{ isTrusted?: (serverUrl: string, hostId: string) => boolean,
 *   add?: (serverUrl: string, hostId: string) => boolean }} deps
 */
function createTrustedRemoteHostRequests({ isTrusted, add } = {}) {
  const declined = new Set();
  const pending = new Map();

  const keyFor = (serverUrl, hostId) => {
    const server = trustedRemoteTarget(serverUrl);
    const host = normalizeTrustedHostId(hostId);
    if (server === null || host === null || !TRUSTED_HOST_INPUT_RE.test(host)) return null;
    return JSON.stringify([server, host]);
  };

  return {
    /** Ask once whether this host's sessions may open localhost pages. */
    request(serverUrl, hostId, approve) {
      const key = keyFor(serverUrl, hostId);
      if (key === null) return Promise.resolve(false);
      if (isTrusted?.(serverUrl, hostId) === true) return Promise.resolve(true);
      if (declined.has(key)) return Promise.resolve(false);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight;
      const attempt = Promise.resolve()
        .then(() => approve())
        .catch(() => null)
        .then((answer) => {
          if (answer === false) declined.add(key);
          if (answer !== true) return false;
          try {
            return add?.(serverUrl, hostId) === true || isTrusted?.(serverUrl, hostId) === true;
          } catch {
            return false;
          }
        })
        .finally(() => pending.delete(key));
      pending.set(key, attempt);
      return attempt;
    },

    /** Why this host's localhost pages stay blocked, or null. */
    hint(serverUrl, hostId) {
      const key = keyFor(serverUrl, hostId);
      if (key === null || !declined.has(key)) return null;
      return "Trust for this host was declined; restart Omnigent to be asked again.";
    },
  };
}

module.exports = { createTrustedRemoteHostRequests };
