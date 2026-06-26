// Hiding the Databricks workspace navigation chrome around a workspace-hosted
// Omnigent SPA. Kept in its own Electron-free module so the injection logic is
// unit-testable (test/workspace-chrome.test.js calls applyWorkspaceChromeHideCss
// with a fake webContents) without requiring main.js, which boots the app.

/**
 * CSS that hides the Databricks workspace navigation chrome.
 *
 * On a workspace the SPA is mounted as a workspace *page*, so Databricks wraps
 * it in its top-nav shell (the dark bar with the workspace switcher). In a
 * dedicated desktop window that chrome is just noise. We promote Omnigent's
 * own root — ``.omnigent-app``, the wrapper ap-web's embed entry sets
 * (``ap-web/src/embed.tsx``) — to a full-viewport overlay so it paints over
 * the workspace bar. Keying on Omnigent's wrapper (defined in THIS repo)
 * rather than the monolith-owned, unstable workspace nav markup keeps this
 * from silently breaking when Databricks reshuffles its chrome; on a
 * standalone (non-embed) build there is no ``.omnigent-app``, so the rule is
 * a harmless no-op.
 */
const WORKSPACE_CHROME_HIDE_CSS = `
  .omnigent-app {
    position: fixed !important;
    inset: 0 !important;
    z-index: 2147483647 !important;
  }
`;

/**
 * Inject the chrome-hide CSS into a finished-loading webContents.
 *
 * Injection is UNCONDITIONAL by design. An earlier version gated this behind
 * ``pathname.startsWith(WORKSPACE_UI_PATH)``, which silently skipped injection
 * whenever the loaded URL didn't match the mount path (auth redirects, path
 * variants) and left the workspace switcher visible. Because the CSS only
 * targets ``.omnigent-app`` — which exists solely in the workspace-embedded
 * build — injecting on every load is a harmless no-op on standalone servers.
 * Do not reintroduce a URL/path guard here.
 *
 * @param {{ insertCSS: (css: string) => Promise<unknown> }} webContents
 */
function applyWorkspaceChromeHideCss(webContents) {
  void webContents.insertCSS(WORKSPACE_CHROME_HIDE_CSS);
}

/**
 * Wire chrome-hide injection to a window's webContents.
 *
 * The CSS is (re)injected on every ``did-finish-load`` — a full document load
 * such as the initial navigation or a server switch. The SPA's own client-side
 * routing keeps the same document, so the injected stylesheet persists across
 * in-app navigation without re-firing.
 *
 * @param {{ on: (event: string, listener: () => void) => void,
 *           insertCSS: (css: string) => Promise<unknown> }} webContents
 */
function registerWorkspaceChromeHide(webContents) {
  webContents.on("did-finish-load", () => {
    applyWorkspaceChromeHideCss(webContents);
  });
}

module.exports = {
  WORKSPACE_CHROME_HIDE_CSS,
  applyWorkspaceChromeHideCss,
  registerWorkspaceChromeHide,
};
