/**
 * Shared render helper for the Omnigent editor panel.
 *
 * The Omnigent UI renders in the editor-beside `WebviewPanel`
 * (EditorPanelController → ViewColumn.Beside) as a single <iframe> pointed at
 * the running LOCAL server. This module factors the render logic into a single
 * `renderInto(webview, opts)` so the controller stays thin.
 *
 * Render decision: the iframe path is used ONLY for LOCAL servers — a local
 * server needs no auth, so no token ever lands in a navigable URL. Non-local
 * targets are rejected upstream (config.resolveServerTarget) and never reach
 * here; until a local target resolves, the controller shows renderResolvingHtml.
 */
import * as vscode from "vscode";
import * as crypto from "node:crypto";
import { buildCsp } from "./csp";
import { buildIframeHtml } from "./iframeHtml";
import type { ServerTarget } from "../config";

/** Returns true when the iframe path should be used for this target. */
export function shouldUseIframe(target: ServerTarget): boolean {
  return target.hostType === "local";
}

export interface RenderIntoOptions {
  /** Resolved (local) server target. */
  target: ServerTarget;
  /** Optional diagnostic logger. */
  log?: (msg: string) => void;
}

/** Render the Omnigent iframe host into a webview. Sets `webview.html`. */
export function renderInto(webview: vscode.Webview, opts: RenderIntoOptions): void {
  const nonce = crypto.randomBytes(16).toString("base64url");
  const csp = buildCsp({
    serverOrigin: opts.target.origin,
    nonce,
    cspSource: webview.cspSource,
  });
  webview.html = buildIframeHtml({ baseUrl: opts.target.baseUrl, csp, nonce });
  opts.log?.(
    `[omnigent] iframe rendered (origin=${opts.target.origin}, nonce=${nonce.slice(0, 8)}...)`,
  );
}

/** Minimal placeholder HTML shown until the server target is resolved. Self-contained CSP. */
export function renderResolvingHtml(): string {
  const csp = "default-src 'none'; style-src 'unsafe-inline'";
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <title>Omnigent</title>
  <style>
    html, body { margin: 0; padding: 0; height: 100%; width: 100%; }
    body {
      display: flex; align-items: center; justify-content: center;
      font-family: var(--vscode-font-family, sans-serif);
      color: var(--vscode-descriptionForeground, #999);
      background: var(--vscode-editor-background, #1e1e1e);
    }
  </style>
</head>
<body>
  <p>Resolving Omnigent server…</p>
</body>
</html>`;
}
