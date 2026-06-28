/**
 * CSP string construction for the iframe host page (pure, unit-testable).
 *
 * The webview hosts a single <iframe> pointed at the running LOCAL Omnigent
 * server. This CSP governs only the HOST page — a nonce'd <style> plus the
 * <iframe> element. The framed application runs in a separate browsing context
 * governed by the *server's* own response headers, not this policy, so the host
 * page needs only three directives:
 *
 *  - default-src 'none' — deny everything not explicitly allowed.
 *  - style-src 'nonce-{nonce}' [cspSource] — the host page's single inline
 *    <style> is nonce'd; cspSource is added when running inside a real webview.
 *  - frame-src {serverOrigin} — allow framing the running server (a separate
 *    origin), which is the entire purpose of the host page.
 *
 * (No script-src/connect-src/worker-src/img-src: the static host page loads no
 * scripts, opens no sockets, and renders no images of its own.)
 */

export interface BuildCspOptions {
  /** Resolved server API origin (e.g. "http://127.0.0.1:6767"). */
  serverOrigin: string;
  /** The cryptographic nonce for this webview load (fresh per render). */
  nonce: string;
  /**
   * VS Code's webview.cspSource value — the allowlist for the extension's own
   * resources. When not in a real webview (tests), omit or pass undefined.
   */
  cspSource?: string;
}

/** Build a strict CSP string for the iframe host page. Pure — no side effects. */
export function buildCsp(opts: BuildCspOptions): string {
  const { serverOrigin, nonce, cspSource } = opts;

  const styleSrc = cspSource ? `'nonce-${nonce}' ${cspSource}` : `'nonce-${nonce}'`;

  const directives: string[] = [
    `default-src 'none'`,
    `style-src ${styleSrc}`,
    `frame-src ${serverOrigin}`,
  ];

  return directives.join("; ");
}
