// Preloaded before main.js (launchDesktop `preload`) so the Arca connect
// console can be reached against a local test server: main.js reads these
// gates at load time from a macOS Managed Preference and a Databricks-host
// check, neither of which a Linux test box can satisfy.
"use strict";

const path = require("node:path");

const SRC = path.resolve(__dirname, "..", "..", "src");
const managedPreferences = require(path.join(SRC, "managed_preferences"));
const url = require(path.join(SRC, "url"));

const allowed = process.env.OMNIGENT_E2E_ARCA_SERVER_URL;
if (!allowed) {
  throw new Error("arcaFeatureGates: OMNIGENT_E2E_ARCA_SERVER_URL is required");
}
const allowedOrigin = new URL(allowed).origin;
const isDatabricksManagedServerUrl = url.isDatabricksManagedServerUrl;

managedPreferences.getDatabricksInternalFeaturesEnabled = () => true;
url.isDatabricksManagedServerUrl = (rawUrl) => {
  try {
    if (new URL(rawUrl).origin === allowedOrigin) return true;
  } catch {
    // Not a URL; fall through to the real check.
  }
  return isDatabricksManagedServerUrl(rawUrl);
};
