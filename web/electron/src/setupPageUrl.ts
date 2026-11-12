// Setup page entry: publishes the shared URL helpers as window.omnigentUrl
// for the page's inline script. Bundled to app/setupPageUrl.js.

import * as omnigentUrl from "./url.ts";

type OmnigentUrl = typeof omnigentUrl;

declare global {
  var omnigentUrl: OmnigentUrl;
}

globalThis.omnigentUrl = omnigentUrl;
