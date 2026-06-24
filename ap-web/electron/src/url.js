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
    } catch (e) {
      throw new Error(`invalid URL: ${e.message}`);
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

  return {
    LOCAL_HOSTS,
    defaultSchemeFor,
    normalizeUrl,
    isPlainHttpRemote,
    WORKSPACE_UI_PATH,
    WORKSPACE_PROBE_TIMEOUT_MS,
    expandDatabricksWorkspaceUrl,
  };
});
