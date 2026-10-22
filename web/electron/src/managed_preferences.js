"use strict";

/** Public macOS Managed Preferences key in the ai.omnigent.desktop domain. */
const SERVER_URLS_KEY = "serverUrls";

/** Optional query parameter on a serverUrls entry naming that server. */
const SERVER_NAME_PARAM = "omnigentServerName";

/**
 * Managed Preferences key gating Databricks-internal features (e.g. the Arca
 * host option). Boolean; anything but an explicit true reads as disabled.
 */
const DATABRICKS_INTERNAL_FEATURES_KEY = "databricksInternalFeaturesEnabled";

/** Keep organization-provided choices bounded on the connect screen. */
const MAX_SERVER_URLS = 10;

/**
 * Normalize one administrator-provided server URL while preserving its path,
 * splitting off its optional display name. Managed servers require TLS; a
 * schemeless host defaults to https://.
 *
 * @param {string} value
 * @returns {{ url: string, name: string | null }}
 */
function parseManagedServerEntry(value) {
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
  const name = url.searchParams.get(SERVER_NAME_PARAM)?.trim() || null;
  // Rewriting the query re-encodes its other params, so only touch it when needed.
  if (url.searchParams.has(SERVER_NAME_PARAM)) url.searchParams.delete(SERVER_NAME_PARAM);
  return { url: url.toString(), name };
}

/**
 * Validate the serverUrls preference as one configuration. An invalid type,
 * entry, or oversized list rejects the whole value rather than applying a
 * surprising partial policy.
 *
 * @param {unknown} value
 * @returns {{ url: string, name: string | null }[]}
 */
function parseManagedServers(value) {
  if (value == null) return [];
  if (!Array.isArray(value) || value.length > MAX_SERVER_URLS) return [];

  const servers = [];
  const origins = new Set();
  try {
    for (const entry of value) {
      const server = parseManagedServerEntry(entry);
      const origin = new URL(server.url).origin;
      if (origins.has(origin)) continue;
      origins.add(origin);
      servers.push(server);
    }
  } catch {
    return [];
  }
  return servers;
}

/**
 * @param {unknown} value
 * @returns {string[]}
 */
function parseManagedServerUrls(value) {
  return parseManagedServers(value).map((server) => server.url);
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
 * @returns {{ url: string, name: string | null }[]}
 */
function readManagedServers({ platform = process.platform, getUserDefault } = {}) {
  if (platform !== "darwin" || typeof getUserDefault !== "function") return [];
  try {
    return parseManagedServers(getUserDefault(SERVER_URLS_KEY, "array"));
  } catch {
    return [];
  }
}

/**
 * @param {Parameters<typeof readManagedServers>[0]} [options]
 * @returns {string[]}
 */
function getManagedServerUrls(options) {
  return readManagedServers(options).map((server) => server.url);
}

/**
 * Display names for the managed servers, keyed by normalized server URL.
 *
 * @param {Parameters<typeof readManagedServers>[0]} [options]
 * @returns {Record<string, string>}
 */
function getManagedServerNames(options) {
  return Object.fromEntries(
    readManagedServers(options).flatMap(({ url, name }) => (name ? [[url, name]] : [])),
  );
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
  SERVER_NAME_PARAM,
  SERVER_URLS_KEY,
  excludingManagedServers,
  getDatabricksInternalFeaturesEnabled,
  getManagedServerNames,
  getManagedServerUrls,
  parseManagedServerUrls,
};
