// Shared URL-normalization helpers for the desktop shell.
//
// Loaded by both the Electron main process (`require("./url")` in
// `src/main.js`) and the bundled setup page (`<script src="../src/url.js">` in
// `setup/index.html`, where it publishes `window.omnigentUrl`). One copy keeps
// the two from drifting — the setup page's plain-http warning and the main
// process's navigation must agree on what a bare URL means.
//
// Only web/Node globals (URL, fetch, AbortSignal) are used, so the same source
// runs unchanged under CommonJS (main) and in the renderer (setup page).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.omnigentUrl = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /**
   * Hostnames that resolve to the local machine. A schemeless URL defaults to
   * https:// (the workspace / remote case the internal user guide documents),
   * but these default to http:// — local dev servers are virtually always plain
   * http, and the setup placeholder shows http://localhost.
   */
  const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

  /**
   * The scheme a schemeless input should default to: http:// for loopback
   * hosts (local dev is plain http), https:// for everything else (the pasted
   * workspace-URL case). Unparseable input falls back to https:// so the
   * caller's own URL parse raises the real error.
   *
   * @param {string} trimmed A trimmed, scheme-less `host[:port][/path]`.
   * @returns {"http" | "https"}
   */
  function defaultSchemeFor(trimmed) {
    let host;
    try {
      host = new URL(`https://${trimmed}`).hostname;
    } catch {
      host = "";
    }
    return LOCAL_HOSTS.has(host) ? "http" : "https";
  }

  /**
   * Normalize a user-entered server URL into something navigable. Accepts a
   * bare `host[:port][/path]` and defaults the scheme (https://, or http:// for
   * loopback hosts), trims whitespace, and rejects anything that isn't an
   * http(s) URL — fail loud rather than navigate to garbage.
   *
   * @param {string} raw
   * @returns {string} A normalized absolute http(s) URL.
   */
  function normalizeUrl(raw) {
    const trimmed = (raw ?? "").trim();
    if (trimmed === "") throw new Error("server URL is empty");
    const withScheme = trimmed.includes("://")
      ? trimmed
      : `${defaultSchemeFor(trimmed)}://${trimmed}`;
    let url;
    try {
      url = new URL(withScheme);
    } catch (error) {
      throw new Error(`invalid URL: ${error.message}`, { cause: error });
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error(`unsupported scheme '${url.protocol}' (use http/https)`);
    }
    return url.toString();
  }

  /**
   * True when the entered URL is unencrypted http:// to a non-local host — the
   * setup page warns before connecting. Mirrors normalizeUrl's scheme-
   * defaulting (https:// by default, http:// for loopback), so a bare remote
   * host — now https — does not trip the warning; only an explicit http:// to a
   * remote host does. Invalid URLs return false so the real error comes from
   * normalizeUrl on Connect.
   *
   * @param {string} raw
   * @returns {boolean}
   */
  function isPlainHttpRemote(raw) {
    const trimmed = (raw || "").trim();
    if (trimmed === "") return false;
    const withScheme = trimmed.includes("://")
      ? trimmed
      : `${defaultSchemeFor(trimmed)}://${trimmed}`;
    let url;
    try {
      url = new URL(withScheme);
    } catch {
      return false;
    }
    return url.protocol === "http:" && !LOCAL_HOSTS.has(url.hostname);
  }

  /**
   * Path under a Databricks workspace where the Omnigent web UI is mounted. A
   * bare workspace URL serves the workspace's own web app at the root, so a
   * user who pastes just the workspace host (e.g.
   * ``https://<ws>.azuredatabricks.net``) lands on a 404 unless this suffix is
   * appended.
   *
   * NOTE: the Python CLI records the UI mount as ``/omnigent`` in
   * ``omnigent/conversation_browser.py`` (WORKSPACE_UI_PATH), whereas the
   * desktop deliberately keeps ``/ml/omnigents`` for now — that is the path the
   * live workspace serves the embedded SPA on. The two are intentionally
   * divergent pending reconciliation; do not "fix" this to ``/omnigent``
   * without verifying what the workspace actually serves to the desktop shell.
   */
  const WORKSPACE_UI_PATH = "/ml/omnigents";

  /**
   * Databricks Apps are served from ``*.databricksapps.com`` and answer with the
   * same ``server: databricks`` header as a workspace, but they are NOT
   * workspaces and have no ``/ml/omnigents`` mount. Skip expansion for these
   * hosts so a user who points the shell at a Databricks App is left on the URL
   * they entered.
   */
  const DATABRICKS_APPS_HOST_SUFFIX = "databricksapps.com";

  /**
   * Path the Omnigent API is mounted at under a Databricks workspace. The CLI
   * legitimately records ``server: <ws>/api/2.0/omnigent`` in its config, and
   * that same value can end up persisted as the desktop ``server_url``. A
   * top-level navigation to the raw API path with no credential answers
   * ``401 {"...": "Credential was not sent ..."}`` as ``application/json``,
   * dead-ending the window on the raw error with no way back to SSO.
   */
  const API_MOUNT_PATH = "/api/2.0/omnigent";

  /**
   * Strip a trailing ``/api/2.0/omnigent`` (with or without a trailing slash)
   * from a saved server URL, leaving the workspace root. Applied at boot before
   * the window loads its saved ``server_url`` so the shell never navigates to
   * the raw API endpoint (which 401s into a dead-end JSON body). A URL already
   * at the root, or carrying any other path, is returned unchanged.
   *
   * @param {string} rawUrl A saved server URL (may be undefined/empty/garbage).
   * @returns {string} The workspace-root URL when the API mount was stripped,
   *   else the input unchanged.
   */
  function stripApiMountFromServerUrl(rawUrl) {
    if (typeof rawUrl !== "string" || rawUrl === "") return rawUrl;
    let url;
    try {
      url = new URL(rawUrl);
    } catch {
      return rawUrl;
    }
    const stripped = url.pathname.replace(/\/api\/2\.0\/omnigent\/?$/, "");
    if (stripped === url.pathname) return rawUrl;
    url.pathname = stripped;
    // An emptied path serializes back to a bare origin (no trailing slash),
    // matching the workspace-root form the rest of the shell expects.
    return url.pathname === "" || url.pathname === "/" ? url.origin : url.toString();
  }

  /**
   * Probe timeout for Databricks workspace detection. Deliberately short: a
   * slow or unreachable host must not stall the connect flow — on timeout we
   * fall back to loading the URL exactly as entered.
   */
  const WORKSPACE_PROBE_TIMEOUT_MS = 8000;

  /**
   * Expand a bare Databricks workspace URL to its Omnigent web-UI mount.
   *
   * Mirrors the omni CLI's behavioral detection
   * (``omnigent/cli.py:_workspace_api_server_url``): rather than match
   * hostnames, probe the URL and adopt the mount only when the host answers
   * like a Databricks workspace — a response carrying the ``server: databricks``
   * header. URLs that already carry a path, or aren't https, are returned
   * untouched WITHOUT a probe, so a user who pastes the full ``…/ml/omnigents``
   * URL (or connects to any non-workspace server) is never second-guessed.
   *
   * The CLI appends the API mount because it's an API client; the desktop shell
   * loads the web UI, so it appends the SPA mount instead.
   *
   * @param {string} normalized A normalized http(s) URL from normalizeUrl().
   * @returns {Promise<string>} The workspace UI URL when expansion applies,
   *   else the input unchanged.
   */
  async function expandDatabricksWorkspaceUrl(normalized) {
    let url;
    try {
      url = new URL(normalized);
    } catch {
      return normalized;
    }
    // Only bare https roots are candidates: a non-root path means the user
    // already pointed at a specific mount, and Databricks workspaces are
    // https-only.
    if (url.protocol !== "https:" || (url.pathname !== "/" && url.pathname !== "")) {
      return normalized;
    }
    // Databricks Apps share the workspace ``server: databricks`` header but have
    // no ``/ml/omnigents`` mount, so never expand them.
    const host = url.hostname.toLowerCase();
    if (host === DATABRICKS_APPS_HOST_SUFFIX || host.endsWith(`.${DATABRICKS_APPS_HOST_SUFFIX}`)) {
      return normalized;
    }
    let probe;
    try {
      probe = await fetch(`${url.origin}/`, {
        method: "HEAD",
        redirect: "manual",
        signal: AbortSignal.timeout(WORKSPACE_PROBE_TIMEOUT_MS),
      });
    } catch {
      // Unreachable / DNS / TLS / timeout: connect to the URL as given and let
      // the did-fail-load fallback surface any real failure.
      return normalized;
    }
    if ((probe.headers.get("server") ?? "").toLowerCase() !== "databricks") {
      return normalized;
    }
    return `${url.origin}${WORKSPACE_UI_PATH}`;
  }

  /**
   * Path of the server's version manifest (RFC 8615 well-known URI). Served
   * unauthed so the shell can read it before the SPA loads / any login.
   */
  const WELL_KNOWN_MANIFEST_PATH = "/.well-known/omnigent.json";

  /**
   * The manifest a pre-manifest server implies: every server older than the
   * route, which 404s. NOT an error — the shell keeps its existing behavior.
   * `manifestVersion: 0` means "older than version 1", so the ordinary `>=`
   * gate excludes it without callers special-casing null.
   */
  const PRE_MANIFEST_BASELINE = Object.freeze({
    manifestVersion: 0,
    serverVersion: null,
    minDesktopVersion: null,
    ui: Object.freeze({}),
  });

  /**
   * Timeout for the manifest fetch. Short and non-fatal for the same reason as
   * the workspace probe: connecting must never stall behind it. On timeout we
   * fall back to the pre-manifest baseline and connect anyway.
   */
  const MANIFEST_FETCH_TIMEOUT_MS = 5000;

  /**
   * Read a server's version manifest, so the shell can adapt to the server it
   * actually reached instead of assuming its own release's behavior.
   *
   * TOTAL: this never throws and never blocks a connection. Anything short of a
   * well-formed manifest — 404 (older server), unreachable host, HTML from an
   * SPA catch-all, malformed JSON, wrong types — yields
   * {@link PRE_MANIFEST_BASELINE}. "I could not learn anything" and "this
   * server predates the manifest" are deliberately the same answer: both mean
   * "use existing behavior", which is what keeps an older shell working
   * against a newer server and vice versa.
   *
   * Callers gate with `manifestVersion >= N`, never `=== N`, so a server that
   * bumps the envelope stays usable by a shell that predates the bump.
   *
   * @param {string} serverUrl A normalized absolute http(s) server URL.
   * @returns {Promise<{manifestVersion: number, serverVersion: string | null,
   *   minDesktopVersion: string | null, ui: Record<string, unknown>}>}
   */
  async function fetchServerManifest(serverUrl) {
    let origin;
    try {
      origin = new URL(serverUrl).origin;
    } catch {
      return PRE_MANIFEST_BASELINE;
    }
    let response;
    try {
      response = await fetch(`${origin}${WELL_KNOWN_MANIFEST_PATH}`, {
        // A redirect to a login page is not a manifest; don't follow it.
        redirect: "manual",
        signal: AbortSignal.timeout(MANIFEST_FETCH_TIMEOUT_MS),
      });
    } catch {
      return PRE_MANIFEST_BASELINE;
    }
    if (!response.ok) return PRE_MANIFEST_BASELINE;
    // Guard the content type explicitly: a server whose SPA catch-all swallows
    // unknown paths answers 200 text/html, and parsing that as a manifest would
    // be worse than not having one. (Servers with the route also exclude
    // `.well-known` from the SPA fallback, so this is belt-and-braces for
    // proxies and older builds.)
    const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
    if (!contentType.includes("json")) return PRE_MANIFEST_BASELINE;
    let body;
    try {
      body = await response.json();
    } catch {
      return PRE_MANIFEST_BASELINE;
    }
    if (body === null || typeof body !== "object") return PRE_MANIFEST_BASELINE;
    // An envelope whose version isn't a number tells us nothing we can gate
    // on, so treat it as absent rather than trusting the rest of the document.
    if (typeof body.manifest_version !== "number" || !Number.isFinite(body.manifest_version)) {
      return PRE_MANIFEST_BASELINE;
    }
    return {
      manifestVersion: body.manifest_version,
      serverVersion: typeof body.server_version === "string" ? body.server_version : null,
      minDesktopVersion:
        typeof body.min_desktop_version === "string" ? body.min_desktop_version : null,
      // Passed through as-is: unknown keys are the extension point, so the
      // shell must not filter to the ones this release happens to know.
      ui: body.ui !== null && typeof body.ui === "object" ? body.ui : {},
    };
  }

  return {
    LOCAL_HOSTS,
    defaultSchemeFor,
    normalizeUrl,
    isPlainHttpRemote,
    API_MOUNT_PATH,
    stripApiMountFromServerUrl,
    WORKSPACE_UI_PATH,
    WORKSPACE_PROBE_TIMEOUT_MS,
    expandDatabricksWorkspaceUrl,
    WELL_KNOWN_MANIFEST_PATH,
    MANIFEST_FETCH_TIMEOUT_MS,
    PRE_MANIFEST_BASELINE,
    fetchServerManifest,
  };
});
