"use strict";

// SSH aliases a page suggests for a host this desktop has no record for. Main
// uses one only if it is defined in the user's own SSH config, and records it
// only after a call the host checks a token for succeeds over it.

const { execFile } = require("node:child_process");
const { validateDestination } = require("./ssh_connect");

// A suggestion that fails is not tried again for this long.
const RETRY_MS = 5 * 60_000;
const CONFIG_TIMEOUT_MS = 5_000;

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
 * Whether the user's SSH config defines `alias`: it resolves to another host
 * name or goes through a proxy. A bare host name resolves to itself.
 *
 * @param {string} alias
 * @param {{execFile?: typeof execFile}} [deps]
 * @returns {Promise<boolean>}
 */
function isConfiguredSshAlias(alias, deps = {}) {
  const run = deps.execFile ?? execFile;
  return new Promise((resolve) => {
    run("ssh", ["-G", alias], { timeout: CONFIG_TIMEOUT_MS }, (error, stdout) => {
      if (error) return resolve(false);
      const settings = new Map();
      for (const line of String(stdout).split("\n")) {
        const [key, ...value] = line.trim().split(/\s+/);
        if (key) settings.set(key.toLowerCase(), value.join(" "));
      }
      const hostName = (settings.get("hostname") ?? "").toLowerCase();
      const proxied = ["proxycommand", "proxyjump"].some(
        (key) => settings.has(key) && settings.get(key) !== "none",
      );
      resolve(proxied || (hostName !== "" && hostName !== alias.toLowerCase()));
    });
  });
}

/**
 * @param {{
 *   record: (serverUrl: string, hostId: string, alias: string) => void,
 *   isConfigured?: (alias: string) => Promise<boolean>,
 *   now?: () => number,
 * }} deps
 */
function createSshTargetChecks(deps) {
  const isConfigured = deps.isConfigured ?? isConfiguredSshAlias;
  const now = deps.now ?? Date.now;
  const failedUntil = new Map();
  const pending = new Map();

  return {
    /**
     * Resolve true once `alias` proves to reach `hostId` (and is recorded), else false.
     * Concurrent checks of one suggestion share one attempt.
     *
     * @param {string} serverUrl
     * @param {string} hostId
     * @param {string} alias A value `suggestedAlias` accepted.
     * @param {() => Promise<boolean>} proves A token-checked call over `alias`.
     * @returns {Promise<boolean>}
     */
    verify(serverUrl, hostId, alias, proves) {
      const key = JSON.stringify([serverUrl, hostId, alias]);
      if ((failedUntil.get(key) ?? 0) > now()) return Promise.resolve(false);
      const inFlight = pending.get(key);
      if (inFlight) return inFlight;
      const check = (async () => {
        try {
          if ((await isConfigured(alias)) && (await proves())) {
            deps.record(serverUrl, hostId, alias);
            return true;
          }
        } catch {
          // Unreachable, refused or not this host: the read goes through the server.
        } finally {
          pending.delete(key);
        }
        failedUntil.set(key, now() + RETRY_MS);
        return false;
      })();
      pending.set(key, check);
      return check;
    },
  };
}

module.exports = { createSshTargetChecks, isConfiguredSshAlias, suggestedAlias };
