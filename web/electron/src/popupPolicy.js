// Policy for page-initiated window.open / target=_blank (the
// setWindowOpenHandler path in main.js). Default: leave the shell — web
// links open in the user's real browser, non-web schemes need consent.
// The ONE exception is an OAuth sign-in popup: the workspace's OAuth
// callback returns the authorization code via window.opener.postMessage
// plus a nonce in the OPENER's localStorage, both of which exist only in
// a real same-profile child window — an external browser strands the code
// and the flow dies. A popup is allowed only when it is popup-SHAPED
// (explicit width/height features — links and bare window.open arrive as
// "foreground-tab"), the opener window is pinned AND currently ON its
// pinned origin, and the target is https on the pinned origin, a
// well-known OAuth authorization origin, or settings.json
// `popup_allowed_origins`. That last gate means page content can never
// open an arbitrary URL in a chromeless window. Allowed popups are
// further hardened by the caller (hardenOauthPopup in main.js).
//
// Runs in the main process (the gate holds regardless of caller); pure +
// dep-free so `node --test` can exercise it without Electron.

"use strict";

/**
 * Schemes that open externally with no confirmation: they land in the
 * user's browser / mail client, which apply their own safety UX. Anything
 * else launches an OS protocol handler (vscode://, ssh://, …) with
 * page-controlled arguments — and `shell.openExternal`, unlike a browser,
 * shows no prompt of its own — so it goes through a consent dialog first.
 */
const WEB_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/**
 * Origins of the OAuth authorization endpoints behind the workspace's
 * managed connections (mirrors what the workspace UI actually opens).
 * Private deployments extend via settings.json `popup_allowed_origins`.
 */
const OAUTH_POPUP_ORIGINS = new Set([
  "https://github.com", // GitHub (system.ai.github)
  "https://accounts.google.com", // Google Drive / Gmail / Calendar / GA4 / Ads
  "https://slack.com", // Slack (system.ai.slack — MCP authorize lives on slack.com, not mcp.slack.com)
  "https://mcp.atlassian.com", // Atlassian MCP (DCR; authorization server IS the MCP host)
  "https://auth.atlassian.com", // Jira / Confluence (classic ingestion connectors)
  "https://login.microsoftonline.com", // SharePoint / OneDrive / Power BI / Azure SQL
  "https://login.salesforce.com", // Salesforce
  "https://test.salesforce.com", // Salesforce sandbox orgs
]);

/** Popup-shaped features: an explicit width or height entry. */
const SIZE_FEATURE_RE = /(^|,)\s*(width|height)\s*=/i;

/**
 * Decide what to do with a page-initiated window.open / target=_blank.
 *
 * @param {{url: string, disposition?: string, features?: string}} details
 *   Fields from Electron's setWindowOpenHandler.
 * @param {{openerOrigin: string | null, pinnedOrigin: string | null,
 *   extraPopupOrigins?: unknown}} context The opener window's current
 *   top-level origin, its pinned origin, and the raw settings.json
 *   `popup_allowed_origins` value (unvalidated — non-arrays are ignored).
 * @returns {{kind: "popup"} | {kind: "external"}
 *   | {kind: "protocol-consent", scheme: string} | {kind: "ignore"}}
 *   popup → hardened child window; external → shell.openExternal;
 *   protocol-consent → consent dialog; ignore → unparseable URL.
 */
function decideWindowOpen(details, context) {
  let parsed;
  try {
    parsed = new URL(details.url);
  } catch {
    return { kind: "ignore" };
  }
  if (!WEB_SCHEMES.has(parsed.protocol)) {
    return { kind: "protocol-consent", scheme: parsed.protocol };
  }
  const popupShaped =
    details.disposition === "new-window" && SIZE_FEATURE_RE.test(details.features ?? "");
  if (!popupShaped) return { kind: "external" };
  const { openerOrigin, pinnedOrigin } = context;
  if (!pinnedOrigin || openerOrigin !== pinnedOrigin) return { kind: "external" };
  if (parsed.protocol !== "https:") return { kind: "external" };
  if (parsed.origin === pinnedOrigin || OAUTH_POPUP_ORIGINS.has(parsed.origin)) {
    return { kind: "popup" };
  }
  const extra = context.extraPopupOrigins;
  if (Array.isArray(extra) && extra.includes(parsed.origin)) {
    return { kind: "popup" };
  }
  return { kind: "external" };
}

/**
 * Response headers that sever a popup's `window.opener`. A main-frame
 * ``COOP: same-origin`` hop (slack.com's sign-in pages serve one) moves
 * the popup into a new browsing-context group: the opener's handle reports
 * closed=true and the popup's window.opener is permanently nulled — which
 * kills the handshake this popup exists for, and only on FIRST sign-ins
 * (retries skip the sign-in page via the provider session cookie).
 * Report-Only doesn't sever; stripped just to keep violation noise out.
 */
const OPENER_SEVERING_HEADERS = new Set([
  "cross-origin-opener-policy",
  "cross-origin-opener-policy-report-only",
]);

/**
 * Strip opener-severing headers from a webRequest response-headers object.
 *
 * @param {Record<string, string[]> | undefined} responseHeaders
 * @returns {Record<string, string[]> | null} Copy without COOP headers, or
 *   null when there was nothing to strip.
 */
function stripCrossOriginOpenerHeaders(responseHeaders) {
  if (!responseHeaders) return null;
  let found = false;
  const stripped = {};
  for (const [key, value] of Object.entries(responseHeaders)) {
    if (OPENER_SEVERING_HEADERS.has(key.toLowerCase())) {
      found = true;
      continue;
    }
    stripped[key] = value;
  }
  return found ? stripped : null;
}

module.exports = {
  decideWindowOpen,
  stripCrossOriginOpenerHeaders,
  WEB_SCHEMES,
  // Exported for focused unit tests.
  OAUTH_POPUP_ORIGINS,
};
