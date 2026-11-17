"use strict";

// SSH aliases a page suggests for a host this desktop has no record for. Main
// asks the user before it runs SSH with one, and records it only after a call
// the host checks a token for succeeds over it.

const { validateDestination } = require("./ssh_connect");

// An allowed alias that fails is not tried again for this long.
const RETRY_MS = 5 * 60_000;

/** `alias` if it is a plain alias (no user, no options), else null. */
function suggestedAlias(alias) {
  try {
    const valid = typeof alias === "string" ? validateDestination(alias) : null;
    return valid && !valid.includes("@") ? valid : null;
  } catch {
    return null;
  }
}

/**
 * @param {{
 *   record: (serverUrl: string, hostId: string, alias: string) => void,
 *   now?: () => number,
 * }} deps
 */
function createSshTargetChecks(deps) {
  const now = deps.now ?? Date.now;
  const allowed = new Set();
  // Per server and alias, whichever host it was offered for, until restart.
  const declined = new Set();
  const failedUntil = new Map();
  const pending = new Map();
  let asking = false;

  async function check(serverUrl, hostId, alias, { approve, proves }) {
    const key = JSON.stringify([serverUrl, hostId, alias]);
    if (!allowed.has(key)) {
      asking = true;
      const approved = await approve().catch(() => false);
      asking = false;
      if (!approved) {
        declined.add(JSON.stringify([serverUrl, alias]));
        return false;
      }
      allowed.add(key);
    }
    // Unreachable, refused or not this host: the read goes through the server.
    if (!(await proves().catch(() => false))) {
      failedUntil.set(key, now() + RETRY_MS);
      return false;
    }
    try {
      deps.record(serverUrl, hostId, alias);
    } catch (error) {
      console.warn("[omnigent] couldn't record the SSH alias:", error);
    }
    return true;
  }

  return {
    /**
     * Resolve true once the user allows `alias` and it proves to reach `hostId`
     * (and is recorded), else false. Concurrent checks of one suggestion share
     * one attempt, and only one question is open at a time.
     *
     * @param {string} serverUrl
     * @param {string} hostId
     * @param {string} alias A value `suggestedAlias` accepted.
     * @param {{approve: () => Promise<boolean>, proves: () => Promise<boolean>}} steps
     *   Ask the user; then make a token-checked call over `alias`.
     * @returns {Promise<boolean>}
     */
    verify(serverUrl, hostId, alias, steps) {
      const key = JSON.stringify([serverUrl, hostId, alias]);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight;
      if (declined.has(JSON.stringify([serverUrl, alias]))) return Promise.resolve(false);
      if ((failedUntil.get(key) ?? 0) > now()) return Promise.resolve(false);
      failedUntil.delete(key);
      if (asking && !allowed.has(key)) return Promise.resolve(false);
      const attempt = check(serverUrl, hostId, alias, steps).finally(() => pending.delete(key));
      pending.set(key, attempt);
      return attempt;
    },
  };
}

module.exports = { createSshTargetChecks, suggestedAlias };
