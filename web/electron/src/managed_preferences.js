"use strict";

/** Public macOS Managed Preferences key in the ai.omnigent.desktop domain. */
const SERVER_URLS_KEY = "serverUrls";

/**
 * Managed Preferences key gating Databricks-internal features (e.g. the Arca
 * host option). Boolean; anything but an explicit true reads as disabled.
 */
const DATABRICKS_INTERNAL_FEATURES_KEY = "databricksInternalFeaturesEnabled";

/** Keep organization-provided choices bounded on the connect screen. */
const MAX_SERVER_URLS = 10;

/**
 * Normalize one administrator-provided server URL while preserving its path.
 * Managed servers require TLS; a schemeless host defaults to https://.
 *
 * @param {string} value
 * @returns {string}
 */
function normalizeManagedServerUrl(value) {
  if (typeof value !== "string") throw new TypeError("server URL must be a string");
  const trimmed = value.trim();
  if (trimmed === "") throw new Error("server URL is empty");
  const withScheme = trimmed.includes("://") ? trimmed : `https://${trimmed}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch (error) {
    throw new Error(`invalid server URL: ${error.message}`, { cause: error });
  }
  if (url.protocol !== "https:" || url.hostname === "") {
    throw new Error("managed server URLs must use https://");
  }
  return url.toString();
}

/**
 * Validate the serverUrls preference as one configuration. An invalid type,
 * entry, or oversized list rejects the whole value rather than applying a
 * surprising partial policy.
 *
 * @param {unknown} value
 * @returns {string[]}
 */
function parseManagedServerUrls(value) {
  if (value == null) return [];
  if (!Array.isArray(value) || value.length > MAX_SERVER_URLS) return [];

  const urls = [];
  const origins = new Set();
  try {
    for (const entry of value) {
      const normalized = normalizeManagedServerUrl(entry);
      const origin = new URL(normalized).origin;
      if (origins.has(origin)) continue;
      origins.add(origin);
      urls.push(normalized);
    }
  } catch {
    return [];
  }
  return urls;
}

/**
 * Read effective macOS preferences. MDM-forced values and ordinary defaults
 * share NSUserDefaults' effective-value API; callers treat the result as
 * read-only and never copy the configured list into settings.json.
 *
 * @param {{
 *   platform?: NodeJS.Platform,
 *   getUserDefault?: (key: string, type: string) => unknown,
 * }} [options]
 * @returns {string[]}
 */
function getManagedServerUrls({ platform = process.platform, getUserDefault } = {}) {
  if (platform !== "darwin" || typeof getUserDefault !== "function") return [];
  try {
    return parseManagedServerUrls(getUserDefault(SERVER_URLS_KEY, "array"));
  } catch {
    return [];
  }
}

/**
 * Read the Databricks-internal-features flag from effective macOS preferences.
 * Fails closed: any missing value, wrong type, non-darwin platform, or read
 * error reads as disabled. Like the managed server list, the value is read on
 * demand and never persisted, so removing the profile disables it immediately.
 *
 * @param {{
 *   platform?: NodeJS.Platform,
 *   getUserDefault?: (key: string, type: string) => unknown,
 * }} [options]
 * @returns {boolean}
 */
function getDatabricksInternalFeaturesEnabled({
  platform = process.platform,
  getUserDefault,
} = {}) {
  if (platform !== "darwin" || typeof getUserDefault !== "function") return false;
  try {
    return getUserDefault(DATABRICKS_INTERNAL_FEATURES_KEY, "boolean") === true;
  } catch {
    return false;
  }
}

/**
 * Remove user recents whose origin is already supplied by the organization.
 *
 * @param {unknown} candidates
 * @param {string[]} managedServers
 * @returns {string[]}
 */
function excludingManagedServers(candidates, managedServers) {
  if (!Array.isArray(candidates)) return [];
  const managedOrigins = new Set(managedServers.map((url) => new URL(url).origin));
  return candidates.filter((candidate) => {
    if (typeof candidate !== "string") return false;
    try {
      return !managedOrigins.has(new URL(candidate).origin);
    } catch {
      return true;
    }
  });
}

module.exports = {
  DATABRICKS_INTERNAL_FEATURES_KEY,
  MAX_SERVER_URLS,
  SERVER_URLS_KEY,
  excludingManagedServers,
  getDatabricksInternalFeaturesEnabled,
  getManagedServerUrls,
  normalizeManagedServerUrl,
  parseManagedServerUrls,
};
