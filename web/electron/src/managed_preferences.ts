/** macOS Managed Preferences key in the ai.omnigent.desktop domain. */
export const SERVER_URLS_KEY = "serverUrls";

/** Optional query parameter on a serverUrls entry naming that server. */
export const SERVER_NAME_PARAM = "omnigentServerName";

/**
 * Managed Preferences key gating managed features (e.g. the Arca
 * host option). Boolean; anything but an explicit true reads as disabled.
 */
export const DATABRICKS_INTERNAL_FEATURES_KEY = "databricksInternalFeaturesEnabled";

/** Keep organization-provided choices bounded on the connect screen. */
export const MAX_SERVER_URLS = 10;

/** One administrator-provided server and its optional display name. */
export interface ManagedServer {
  url: string;
  name: string | null;
}

/** Where effective preferences come from; `getUserDefault` is Electron's `systemPreferences.getUserDefault`. */
export interface ManagedPreferenceOptions {
  platform?: NodeJS.Platform;
  getUserDefault?: ((key: string, type: string) => unknown) | undefined;
}

/**
 * Normalize one administrator-provided server URL while preserving its path,
 * splitting off its optional display name. Managed servers require TLS; a
 * schemeless host defaults to https://.
 */
function parseManagedServerEntry(value: unknown): ManagedServer {
  if (typeof value !== "string") throw new TypeError("server URL must be a string");
  const trimmed = value.trim();
  if (trimmed === "") throw new Error("server URL is empty");
  const withScheme = trimmed.includes("://") ? trimmed : `https://${trimmed}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch (error) {
    throw new Error(`invalid server URL: ${(error as Error).message}`, { cause: error });
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
 */
function parseManagedServers(value: unknown): ManagedServer[] {
  if (value == null) return [];
  if (!Array.isArray(value) || value.length > MAX_SERVER_URLS) return [];

  const servers: ManagedServer[] = [];
  const origins = new Set<string>();
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

export function parseManagedServerUrls(value: unknown): string[] {
  return parseManagedServers(value).map((server) => server.url);
}

/**
 * Read effective macOS preferences. Forced values and ordinary defaults
 * share NSUserDefaults' effective-value API; callers treat the result as
 * read-only and never copy the configured list into settings.json.
 */
function readManagedServers({
  platform = process.platform,
  getUserDefault,
}: ManagedPreferenceOptions = {}): ManagedServer[] {
  if (platform !== "darwin" || typeof getUserDefault !== "function") return [];
  try {
    return parseManagedServers(getUserDefault(SERVER_URLS_KEY, "array"));
  } catch {
    return [];
  }
}

export function getManagedServerUrls(options?: ManagedPreferenceOptions): string[] {
  return readManagedServers(options).map((server) => server.url);
}

/**
 * Display names for the managed servers, keyed by normalized server URL.
 */
export function getManagedServerNames(options?: ManagedPreferenceOptions): Record<string, string> {
  return Object.fromEntries(
    readManagedServers(options).flatMap(({ url, name }) => (name ? [[url, name] as const] : [])),
  );
}

/**
 * Read the managed-features flag from effective macOS preferences.
 * Fails closed: any missing value, wrong type, non-darwin platform, or read
 * error reads as disabled. Like the managed server list, the value is read on
 * demand and never persisted, so removing the profile disables it immediately.
 */
export function getDatabricksInternalFeaturesEnabled({
  platform = process.platform,
  getUserDefault,
}: ManagedPreferenceOptions = {}): boolean {
  if (platform !== "darwin" || typeof getUserDefault !== "function") return false;
  try {
    return getUserDefault(DATABRICKS_INTERNAL_FEATURES_KEY, "boolean") === true;
  } catch {
    return false;
  }
}

/**
 * Remove user recents whose origin is already supplied by the organization.
 */
export function excludingManagedServers(candidates: unknown, managedServers: string[]): string[] {
  if (!Array.isArray(candidates)) return [];
  const managedOrigins = new Set(managedServers.map((url) => new URL(url).origin));
  return candidates.filter((candidate): candidate is string => {
    if (typeof candidate !== "string") return false;
    try {
      return !managedOrigins.has(new URL(candidate).origin);
    } catch {
      return true;
    }
  });
}
