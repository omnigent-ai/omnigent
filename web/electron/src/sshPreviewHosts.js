"use strict";

// Localhost previews require a proven SSH destination and consent for this run.
// A new destination is verified only after the user agrees to the SSH check.

const { canonicalHostId } = require("./sshHosts");
const { suggestedAlias } = require("./sshTargets");

/**
 * @param {{ destination: (serverUrl: string, hostId: string) => string | null,
 *   verify?: (serverUrl: string, hostId: string, destination: string) => Promise<boolean>,
 *   record?: (serverUrl: string, hostId: string, destination: string) => void }} deps
 *   Read a known SSH destination, or prove and record it for previews only.
 */
function createSshPreviewHosts({ destination, verify, record }) {
  // Allowed hosts, each with the destination the user saw when allowing it.
  const allowed = new Map();
  // "Not now" holds until restart, so a retrying agent can't keep asking.
  const declined = new Set();
  const pending = new Map();
  const failed = new Map();

  const keyFor = (serverUrl, hostId) => JSON.stringify([serverUrl, canonicalHostId(hostId)]);

  function recorded(serverUrl, hostId) {
    if (typeof serverUrl !== "string" || typeof hostId !== "string" || !hostId) return null;
    const found = destination(serverUrl, hostId);
    const current = typeof found === "string" && found ? found : null;
    const key = keyFor(serverUrl, hostId);
    // Restoring a replaced destination must not revive its earlier consent.
    if (allowed.get(key) !== current) allowed.delete(key);
    const attempt = pending.get(key);
    if (attempt && current !== attempt.observedDestination) {
      if (current !== attempt.destination) attempt.invalidated = true;
      attempt.observedDestination = current;
    }
    return current;
  }

  return {
    /** Observe destination writes even when no preview is open. */
    destinationChanged(serverUrl, hostId) {
      recorded(serverUrl, hostId);
    },

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
      const key = keyFor(serverUrl, hostId);
      return (
        current !== null && !declined.has(key) && !failed.has(key) && allowed.get(key) === current
      );
    },

    /**
     * Ask once whether `hostId`'s sessions may open localhost pages. Concurrent
     * requests share one question; unrecorded destinations require read-only verification.
     *
     * @param {string | null | undefined} serverUrl
     * @param {string | null | undefined} hostId
     * @param {(destination: string, needsVerification: boolean) => Promise<boolean | null>} approve
     *   Shows the question; null when it couldn't be shown.
     * @param {string | undefined} sshTarget Untrusted route hint, never an identity proof.
     * @returns {Promise<boolean>} Whether the host is allowed now.
     */
    request(serverUrl, hostId, approve, sshTarget) {
      if (typeof serverUrl !== "string" || typeof hostId !== "string" || !hostId) {
        return Promise.resolve(false);
      }
      const existing = recorded(serverUrl, hostId);
      const key = keyFor(serverUrl, hostId);
      if (declined.has(key) || failed.has(key)) return Promise.resolve(false);
      if (existing !== null && allowed.get(key) === existing) return Promise.resolve(true);
      const asked = existing ?? (verify && record ? suggestedAlias(sshTarget) : null);
      if (asked === null) return Promise.resolve(false);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight.promise;
      const attempt = { destination: asked, observedDestination: existing, invalidated: false };
      attempt.promise = Promise.resolve()
        .then(() => (attempt.invalidated ? null : approve(asked, existing === null)))
        .catch(() => null)
        .then(async (answer) => {
          if (answer === false) {
            allowed.delete(key);
            declined.add(key);
          }
          if (answer !== true || attempt.invalidated) return false;
          if (existing === null) {
            const verified = await verify(serverUrl, hostId, asked).catch(() => false);
            if (attempt.invalidated) return false;
            if (!verified) {
              failed.set(
                key,
                "The SSH destination could not be verified as this session's host. Check its connection and restart Omnigent to retry.",
              );
              return false;
            }
            // Another connection may have recorded a different destination during verification.
            const current = recorded(serverUrl, hostId);
            if (attempt.invalidated || (current !== null && current !== asked)) return false;
            try {
              record(serverUrl, hostId, asked);
            } catch (error) {
              console.warn("[omnigent] couldn't save the SSH preview destination:", error);
              failed.set(
                key,
                "The verified SSH destination could not be saved. Check desktop storage and restart Omnigent to retry.",
              );
              return false;
            }
          }
          // The record can change while the question is open; the answer covers only what was shown.
          if (recorded(serverUrl, hostId) !== asked || attempt.invalidated) return false;
          allowed.set(key, asked);
          return true;
        })
        .finally(() => pending.delete(key));
      pending.set(key, attempt);
      return attempt.promise;
    },

    /**
     * Why a host's localhost pages stay blocked, or null.
     *
     * @param {string | null | undefined} serverUrl
     * @param {string | null | undefined} hostId
     * @returns {string | null}
     */
    hint(serverUrl, hostId) {
      const failure = failed.get(keyFor(serverUrl, hostId));
      if (failure) return failure;
      const current = recorded(serverUrl, hostId);
      if (!declined.has(keyFor(serverUrl, hostId))) return null;
      return `Localhost previews${current ? ` from "${current}"` : " for this host"} were declined; restart Omnigent to be asked again.`;
    },
  };
}

module.exports = { createSshPreviewHosts };
