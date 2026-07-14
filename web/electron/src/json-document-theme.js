// Chromium's generated JSON viewer follows the OS color scheme. Keep raw JSON
// responses readable without changing the setup page or SPA theme.

"use strict";

/**
 * Apply light colors only when `doc` is a browser-generated JSON document.
 *
 * @param {Document} doc
 * @returns {boolean} Whether the document was changed.
 */
function forceLightJsonDocument(doc) {
  const type = String(doc?.contentType ?? "").toLowerCase();
  if (type !== "application/json" && type !== "text/json" && !type.endsWith("+json")) {
    return false;
  }
  const root = doc.documentElement;
  if (!root) return false;

  root.style.colorScheme = "only light";
  root.style.backgroundColor = "#fff";
  root.style.color = "#111";
  if (doc.body) {
    doc.body.style.backgroundColor = "#fff";
    doc.body.style.color = "#111";
  }
  return true;
}

const FORCE_LIGHT_JSON_DOCUMENT_SCRIPT = `(${forceLightJsonDocument.toString()})(document);`;

/**
 * Reapply the JSON-only theme after each full document navigation.
 *
 * @param {{ on: (event: string, listener: () => void) => void,
 *   executeJavaScript: (script: string) => Promise<unknown> }} webContents
 */
function registerLightJsonDocumentTheme(webContents) {
  webContents.on("dom-ready", () => {
    void webContents.executeJavaScript(FORCE_LIGHT_JSON_DOCUMENT_SCRIPT).catch(() => {});
  });
}

module.exports = {
  FORCE_LIGHT_JSON_DOCUMENT_SCRIPT,
  forceLightJsonDocument,
  registerLightJsonDocumentTheme,
};
