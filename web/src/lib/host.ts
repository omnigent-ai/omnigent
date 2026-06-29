import type { ReactNode } from "react";

/**
 * Embed host integration seam.
 *
 * Standalone web talks to a same-origin server: API calls use relative
 * `/v1/...` paths and the terminal WebSocket is built from
 * `window.location`. When web is embedded as a component library inside
 * another app (e.g. the Databricks monolith), the host injects a config so
 * those calls are rebased onto the host's API surface and auth.
 *
 * Default (no config set) preserves the standalone behavior, so importing
 * this module is a no-op until `setOmnigentHostConfig` is called by the
 * embed entry (`embed.tsx`).
 */

/**
 * A user the host suggests for a permission grant. `userId` is the value
 * actually granted (e.g. an email); `displayName` is an optional human-readable
 * label shown alongside it.
 */
export interface UserSuggestion {
  userId: string;
  displayName?: string;
}

export interface OmnigentHostConfig {
  /**
   * Maps an web API path (always starting with `/v1`, `/health`, or
   * `/api/...`) to a `Response`. The host implementation is responsible for
   * prefixing the real API base and attaching auth (e.g. the monolith's
   * `workspaceFetch` against `/ajax-api/2.0/omnigent`). When omitted, the
   * native `fetch` is used with the path unchanged.
   */
  fetcher?: (path: string, init?: RequestInit) => Promise<Response>;
  /**
   * Optional user search/autocomplete provider. When supplied by the host, the
   * permissions "add user" field becomes a suggestion combobox; when omitted
   * (standalone, or before the host wires it), that field stays a plain text
   * input and this feature is fully inert. The host owns the actual search
   * logic and returns the suggestions to display.
   */
  searchUsers?: (query: string, options?: { signal?: AbortSignal }) => Promise<UserSuggestion[]>;
  /**
   * Maps an web WS path (e.g.
   * `/v1/sessions/{id}/resources/terminals/{tid}/attach`) to a fully
   * qualified `ws(s)://` URL. When omitted, the URL is built from
   * `window.location`.
   */
  resolveWebSocketUrl?: (path: string) => string;
  /**
   * Turns a relative share-link path (basename already included, e.g.
   * `<basename>/c/:id`) into the full absolute share URL — the host prepends
   * its origin and adds any query params it needs. When omitted (standalone),
   * web prepends `window.location.origin` itself.
   */
  transformShareLink?: (relativePath: string) => string;
  /**
   * Path suffix appended to the origin in CLI `--server` instructions shown
   * in the UI (e.g. `"/api/2.0/omnigent"`). When the host proxies the
   * Omnigent API behind a path prefix, CLI users need the full URL
   * (`https://host/api/2.0/omnigent`) — this suffix supplies the
   * non-origin part.
   */
  cliServerUrlSuffix?: string;
  /**
   * Optional documentation links for embed-only UX hints.
   *
   * Standalone web ignores these values. Embedded hosts can pass one object
   * instead of adding many top-level props as docs surfaces grow.
   */
  docsLinks?: {
    /**
     * Full tooltip content shown for the disabled New Sandbox help icon.
     */
    newSandbox?: ReactNode;
    /**
     * Full tooltip content shown for the Databricks git-credentials help icon
     * in the sandbox repository popover.
     */
    databricksGitCredentials?: ReactNode;
  };
}

let _config: OmnigentHostConfig = {};
let _embedRoot: HTMLElement | null = null;

export function setOmnigentHostConfig(config: OmnigentHostConfig): void {
  // Guard: never clobber an already-installed fetcher with an empty config.
  // `OmnigentApp` installs config during render and React may re-invoke it with
  // default/empty props on concurrent or Suspense renders; without this guard
  // such a render would wipe the host transport and API calls would fall back
  // to bare same-origin paths.
  if (!config?.fetcher && _config.fetcher) return;
  _config = config ?? {};
}

export function getOmnigentHostConfig(): OmnigentHostConfig {
  return _config;
}

/**
 * The host-provided user search function, or `undefined` when none is
 * configured. Consumers use the absence to stay inert (plain text input).
 */
export function getOmnigentUserSearch(): OmnigentHostConfig["searchUsers"] {
  return _config.searchUsers;
}

/**
 * The host-provided share-link transform, or `undefined` when none is
 * configured. Absence means the relative URL is used unchanged.
 */
export function getOmnigentTransformShareLink(): OmnigentHostConfig["transformShareLink"] {
  return _config.transformShareLink;
}

/**
 * The DOM node the embed is mounted into. Used as the portal container for
 * Radix overlays so portaled content (dialogs, popovers, tooltips, menus)
 * lands inside the scoped `.omnigent-app` subtree and inherits its styles.
 * Returns null in standalone mode, where Radix falls back to `document.body`.
 */
export function setEmbedRoot(el: HTMLElement | null): void {
  _embedRoot = el;
}

export function getEmbedRoot(): HTMLElement | null {
  return _embedRoot;
}

/**
 * Single network choke point. Delegates to the host fetcher when embedded,
 * otherwise calls native `fetch` with the path unchanged (standalone).
 */
export function hostFetch(path: string, init?: RequestInit): Promise<Response> {
  if (_config.fetcher) {
    return _config.fetcher(path, init);
  }
  return fetch(path, init);
}

export function resolveWebSocketUrl(path: string): string {
  if (_config.resolveWebSocketUrl) {
    return _config.resolveWebSocketUrl(path);
  }
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${path}`;
}

/**
 * Full server URL for CLI `--server` flags shown in in-product docs.
 * Returns `window.location.origin` plus the optional
 * {@link OmnigentHostConfig.cliServerUrlSuffix}.
 */
export function getCliServerUrl(): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  return origin + (_config.cliServerUrlSuffix ?? "");
}
