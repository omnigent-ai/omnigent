"use strict";

// SSH aliases a page suggests for a host this desktop has no record for. Main
// asks the user before it runs SSH with one, and records it only after a call
// the host checks a token for succeeds over it.

const { canonicalHostId } = require("./sshHosts");
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
  // Servers the user said "Not now" to, so a server can't keep asking with new names.
  const declined = new Set();
  // Also kept here, so a record that fails to save isn't probed on every read.
  const proven = new Set();
  const failedUntil = new Map();
  const pending = new Map();
  // One question at a time; later ones wait for it, then skip if their server was declined.
  let questions = Promise.resolve();

  function ask(serverUrl, approve) {
    // null: the question couldn't be shown, so nothing is remembered.
    const answer = questions
      .then(() => (declined.has(serverUrl) ? false : approve()))
      .catch(() => null);
    questions = answer;
    return answer;
  }

  async function check(key, serverUrl, hostId, alias, { approve, proves }) {
    if (!allowed.has(key)) {
      const answer = await ask(serverUrl, approve);
      if (answer !== true) {
        if (answer === false) declined.add(serverUrl);
        return false;
      }
      allowed.add(key);
    }
    const proof = await proves().catch(() => false);
    // A refused token (one that expired during the question, say) is the page's to renew.
    if (proof === "refused") return proof;
    // Unreachable or not this host: the read goes through the server.
    if (!proof) {
      failedUntil.set(key, now() + RETRY_MS);
      return false;
    }
    proven.add(key);
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
     * (and is recorded), "refused" if the host there refused the call's token,
     * else false. Concurrent checks of one suggestion share one attempt, and
     * questions are asked one at a time.
     *
     * @param {string} serverUrl
     * @param {string} hostId
     * @param {string} alias A value `suggestedAlias` accepted.
     * @param {{approve: () => Promise<boolean | null>, proves: () => Promise<boolean | "refused">}} steps
     *   Ask the user (null if it couldn't); then make a token-checked call over `alias`.
     * @returns {Promise<boolean | "refused">}
     */
    verify(serverUrl, hostId, alias, steps) {
      const key = JSON.stringify([serverUrl, canonicalHostId(hostId), alias]);
      if (proven.has(key)) return Promise.resolve(true);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight;
      if (declined.has(serverUrl)) return Promise.resolve(false);
      if ((failedUntil.get(key) ?? 0) > now()) return Promise.resolve(false);
      failedUntil.delete(key);
      const attempt = check(key, serverUrl, hostId, alias, steps).finally(() =>
        pending.delete(key),
      );
      pending.set(key, attempt);
      return attempt;
    },
  };
}

module.exports = { createSshTargetChecks, suggestedAlias };
