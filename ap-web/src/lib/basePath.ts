/**
 * Standalone deployment base-path support.
 *
 * When the Omnigent server is served behind a reverse proxy at a subpath
 * (e.g. code-server's port proxy at `/proxy/6767/`), the SPA must prefix
 * every browser-issued URL — REST/SSE/WebSocket calls, navigation, and
 * static assets — with that subpath so the proxy routes them to the right
 * origin. The server injects the configured base path into `index.html` as
 * `window.__OMNIGENT_BASE_PATH__`; this module reads and applies it.
 *
 * Default (unset / root deployment, and all embedded usage) → `getBasePath()`
 * returns `""` and every helper is an identity, preserving today's behavior.
 *
 * The value is read fresh from `window` on each call: it is fixed for the life
 * of the page in production (set once by the injected script), so re-reading is
 * negligible, and it keeps the helpers free of module-load caching that would
 * be awkward to exercise from tests.
 */

declare global {
  interface Window {
    /** Public base path injected by the server, e.g. `"/proxy/6767"`. */
    __OMNIGENT_BASE_PATH__?: string;
  }
}

/**
 * The normalized base path: a leading-slash, no-trailing-slash string
 * (`"/proxy/6767"`), or `""` for a root deployment.
 */
export function getBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__OMNIGENT_BASE_PATH__;
  if (typeof raw !== "string") return "";
  const trimmed = raw.trim();
  if (trimmed === "" || trimmed === "/") return "";
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeadingSlash.replace(/\/+$/, "");
}

/**
 * Prepend the base path to an app-absolute path (`/v1/...`, `/auth/...`).
 *
 * Idempotent — a path already under the base is returned unchanged — and a
 * no-op for non-absolute inputs (full URLs, `blob:` URLs) and when no base is
 * configured.
 */
export function withBasePath(path: string): string {
  const base = getBasePath();
  if (!base) return path;
  if (!path.startsWith("/")) return path;
  if (path === base || path.startsWith(`${base}/`)) return path;
  return `${base}${path}`;
}

/**
 * Remove the base prefix from a `window.location.pathname` for in-app path
 * comparisons (e.g. "is this the login page?"). Returns `"/"` when the
 * pathname is exactly the base, and leaves a pathname not under the base
 * unchanged.
 */
export function stripBasePath(pathname: string): string {
  const base = getBasePath();
  if (!base) return pathname;
  if (pathname === base) return "/";
  if (pathname.startsWith(`${base}/`)) return pathname.slice(base.length);
  return pathname;
}
