/**
 * Iframe host HTML for the Omnigent editor panel.
 *
 * The webview hosts a single static <iframe> pointed at the running LOCAL
 * Omnigent server — a local server needs no auth, so no token ever appears in
 * the iframe URL (see csp.ts). buildIframeHtml() is a PURE function (no vscode
 * API) so it is unit-testable. The page carries:
 *  1. A strict nonce-based CSP whose `frame-src` allows the server origin.
 *  2. A nonce'd <style> making html/body/#root and the iframe fill 100%.
 *  3. The <iframe src="${baseUrl}"> filling the pane.
 *
 * KNOWN LIMITATION: on macOS, VS Code does not deliver Cmd+A/C/V keystrokes into
 * a cross-origin iframe inside a webview, so keyboard paste into the app's inputs
 * does not work there. This is an unresolved upstream VS Code bug, not fixable
 * from the extension for the iframe render path — see microsoft/vscode#129178 and
 * microsoft/vscode#182642.
 */

export interface BuildIframeHtmlOptions {
  /** Server base URL (e.g. "http://127.0.0.1:6767"). A trailing slash is stripped. */
  baseUrl: string;
  /** The CSP string (from buildCsp) — its frame-src must allow the server origin. */
  csp: string;
  /** Nonce stamped on the inline <style>. */
  nonce: string;
}

export function buildIframeHtml(opts: BuildIframeHtmlOptions): string {
  const { baseUrl, csp, nonce } = opts;
  const src = baseUrl.replace(/\/$/, "");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}" />
  <title>Omnigent</title>
  <style nonce="${nonce}">
    html, body, #root { margin: 0; padding: 0; height: 100%; width: 100%; overflow: hidden; }
    body { background: var(--vscode-editor-background, #1e1e1e); }
    #omnigent-frame { border: 0; width: 100%; height: 100%; display: block; }
  </style>
</head>
<body>
  <div id="root">
    <!--
      allow=clipboard-* delegates the Clipboard API to the framed app, enabling its
      programmatic copy/paste (copy buttons, navigator.clipboard paths) and keystroke
      clipboard on non-macOS. See the macOS keystroke limitation noted above the
      buildIframeHtml docblock (microsoft/vscode#129178, #182642).
    -->
    <iframe id="omnigent-frame" src="${escapeAttr(src)}" allow="clipboard-read; clipboard-write" style="border:0;width:100%;height:100%"></iframe>
  </div>
</body>
</html>`;
}

function escapeAttr(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
