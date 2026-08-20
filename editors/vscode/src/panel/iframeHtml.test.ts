/**
 * Unit tests for buildIframeHtml — the pure, static iframe host page.
 */
import { describe, it, expect } from "vitest";
import { buildIframeHtml } from "./iframeHtml";

const NONCE = "test-nonce-frame";
const CSP = "default-src 'none'; frame-src http://127.0.0.1:6767";

const baseOpts = {
  baseUrl: "http://127.0.0.1:6767",
  csp: CSP,
  nonce: NONCE,
};

describe("buildIframeHtml", () => {
  it("includes a CSP meta tag", () => {
    expect(buildIframeHtml(baseOpts)).toContain('http-equiv="Content-Security-Policy"');
  });

  it("renders an iframe pointed at the base URL", () => {
    const html = buildIframeHtml(baseOpts);
    expect(html).toContain('id="omnigent-frame"');
    expect(html).toContain('src="http://127.0.0.1:6767"');
  });

  it("strips a trailing slash from the base URL", () => {
    const html = buildIframeHtml({ ...baseOpts, baseUrl: "http://127.0.0.1:6767/" });
    expect(html).toContain('src="http://127.0.0.1:6767"');
    expect(html).not.toContain('src="http://127.0.0.1:6767/"');
  });

  it("stamps the nonce on the style", () => {
    expect(buildIframeHtml(baseOpts)).toContain(`<style nonce="${NONCE}">`);
  });

  it("is a static page: no inline script and no vscode api handshake", () => {
    const html = buildIframeHtml(baseOpts);
    expect(html).not.toContain("<script");
    expect(html).not.toContain("acquireVsCodeApi");
    expect(html).not.toContain("omnigent/navigate");
  });

  it("never injects a token into the iframe URL", () => {
    const html = buildIframeHtml(baseOpts);
    expect(html.toLowerCase()).not.toContain("token");
    expect(html).not.toContain("Authorization");
  });

  it("escapes attribute-breaking quotes in the iframe src attribute", () => {
    const html = buildIframeHtml({ ...baseOpts, baseUrl: 'http://x"y' });
    expect(html).not.toContain('src="http://x"y"');
    expect(html).toContain("&quot;");
  });

  it("has a root div filling the pane", () => {
    expect(buildIframeHtml(baseOpts)).toContain('<div id="root">');
  });

  it("delegates clipboard permission to the iframe so copy/paste works in the webview", () => {
    expect(buildIframeHtml(baseOpts)).toContain('allow="clipboard-read; clipboard-write"');
  });
});
