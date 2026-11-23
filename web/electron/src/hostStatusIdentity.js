"use strict";

const { normalizeSavedServerUrl, databricksWorkspaceUiUrl } = require("./url");

/** Canonical selected target, preserving mount and workspace selector. */
function normalizedServerTarget(raw) {
  try {
    const saved = normalizeSavedServerUrl(raw);
    const url = new URL(databricksWorkspaceUiUrl(saved) ?? saved);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.hash = "";
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    url.searchParams.sort();
    return url.href;
  } catch {
    return null;
  }
}

/** Compare status from a CLI command bound to the selected --server target. */
function daemonServesTarget(daemon, serverUrl) {
  const selected = normalizedServerTarget(serverUrl);
  if (!selected) return false;
  const expected = new URL(selected);
  const requestedOrg = expected.searchParams.get("o");
  expected.search = "";
  const target = normalizedServerTarget(daemon.target);
  let daemonTarget = null;
  if (target) {
    const url = new URL(target);
    const org = url.searchParams.get("o");
    // Sign-in may resolve a selector when none was requested; an explicit mismatch is denied.
    if (org === null || requestedOrg === null || org === requestedOrg) {
      url.search = "";
      daemonTarget = url.href;
    }
  }
  // Status may reuse another URL's daemon after verifying the workspace is the same.
  return (
    (daemonTarget === expected.href &&
      normalizedServerTarget(daemon.server_url) === expected.href) ||
    (typeof daemon.same_workspace_as === "string" &&
      normalizedServerTarget(daemon.same_workspace_as) === expected.href)
  );
}

module.exports = { normalizedServerTarget, daemonServesTarget };
