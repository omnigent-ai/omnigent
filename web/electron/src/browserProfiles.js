/**
 * Browser profiles: the storage partitions embedded browser views run on.
 *
 * A profile is a persistent Chromium session (cookies, localStorage, cache,
 * IndexedDB) shared by every view that uses it, across conversations, windows
 * and app restarts — so a sign-in survives like it does in a normal browser.
 * Every view uses the default profile today; named profiles chosen per
 * session, project or tab resolve through `browserProfilePartition` too.
 *
 * Pure + dep-free so `node --test` can exercise it without Electron.
 */

"use strict";

const DEFAULT_BROWSER_PROFILE_ID = "default";

const PROFILE_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

/**
 * Electron partition name for a browser profile. The `persist:` prefix stores
 * the session on disk under the app's userData directory.
 *
 * @param {string} profileId Lowercase letters, digits and dashes.
 * @returns {string}
 */
function browserProfilePartition(profileId) {
  if (typeof profileId !== "string" || !PROFILE_ID_RE.test(profileId)) {
    throw new Error(`Invalid browser profile id: ${JSON.stringify(profileId)}`);
  }
  return `persist:omnigent-browser-${profileId}`;
}

module.exports = {
  DEFAULT_BROWSER_PROFILE_ID,
  browserProfilePartition,
};
