// Hand an embedded-browser page off to the system browser. Google Workspace
// context-aware access only admits browsers with device signals, which an
// Electron view can't report, so a blocked sign-in continues there instead.

"use strict";

// Google's context-aware access remediation page ("Your organization isn't
// allowing access ... only accessible through approved browsers").
const GOOGLE_ACCESS_BLOCKED_HOST = "access.workspace.google.com";

// Hosts that only run Google sign-in or its access checks. Opening them in
// another browser restarts the sign-in instead of reaching the user's page.
const GOOGLE_SIGN_IN_HOSTS = new Set(["accounts.google.com", GOOGLE_ACCESS_BLOCKED_HOST]);

// Main-frame URLs remembered per view since its last explicit navigation.
const NAVIGATION_TRAIL_LIMIT = 30;

/**
 * Parse an http(s) URL, or return null for anything else.
 *
 * @param {unknown} url
 * @returns {URL | null}
 */
function parseWebUrl(url) {
  if (typeof url !== "string" || url === "") return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed : null;
  } catch {
    return null;
  }
}

/** True when `url` is Google's context-aware access block page. */
function isGoogleAccessBlockedUrl(url) {
  return parseWebUrl(url)?.hostname === GOOGLE_ACCESS_BLOCKED_HOST;
}

/** True when `url` is a Google sign-in or access-check page. */
function isGoogleSignInUrl(url) {
  const parsed = parseWebUrl(url);
  return parsed !== null && GOOGLE_SIGN_IN_HOSTS.has(parsed.hostname);
}

/**
 * The page a Google sign-in URL returns to (its `continue` parameter), when
 * that is an http(s) page outside the sign-in flow itself.
 *
 * @param {string} url
 * @returns {string | null}
 */
function signInContinueUrl(url) {
  if (!isGoogleSignInUrl(url)) return null;
  const target = parseWebUrl(new URL(url).searchParams.get("continue"));
  if (!target || GOOGLE_SIGN_IN_HOSTS.has(target.hostname)) return null;
  return target.href;
}

/**
 * Append a main-frame URL to a view's navigation trail, skipping repeats and
 * keeping only the most recent entries.
 *
 * @param {string[]} trail
 * @param {unknown} url
 */
function recordNavigation(trail, url) {
  if (!parseWebUrl(url)) return;
  if (trail[trail.length - 1] === url) return;
  trail.push(url);
  if (trail.length > NAVIGATION_TRAIL_LIMIT) trail.splice(0, trail.length - NAVIGATION_TRAIL_LIMIT);
}

/**
 * The URL to open in the system browser for a view. Normally the current
 * page; on Google's access block page, the page the user was signing in to:
 * the newest sign-in `continue` target, else the first non-sign-in page since
 * the view's last explicit navigation.
 *
 * @param {{ currentUrl: string, navigationTrail?: string[] }} view
 * @returns {string | null} Null when the view shows no http(s) page.
 */
function systemBrowserUrl({ currentUrl, navigationTrail = [] }) {
  const current = parseWebUrl(currentUrl);
  if (!current) return null;
  if (!isGoogleAccessBlockedUrl(current.href)) return current.href;
  const newestFirst = [current.href, ...[...navigationTrail].reverse()];
  for (const url of newestFirst) {
    const target = signInContinueUrl(url);
    if (target) return target;
  }
  const origin = navigationTrail.find((url) => parseWebUrl(url) && !isGoogleSignInUrl(url));
  return origin ?? current.href;
}

module.exports = {
  GOOGLE_ACCESS_BLOCKED_HOST,
  NAVIGATION_TRAIL_LIMIT,
  isGoogleAccessBlockedUrl,
  recordNavigation,
  signInContinueUrl,
  systemBrowserUrl,
};
