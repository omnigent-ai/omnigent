// RFC 8252 loopback authorization in the system browser, shared by desktop
// sign-ins. The caller builds the authorize URL; this module owns the one-shot
// loopback listener, state check, timeout, cancellation, and the landing page.

import crypto from "node:crypto";
import http from "node:http";
import type { AddressInfo } from "node:net";

/** Bound on how long we wait for the human to finish signing in in the browser. */
export const LOOPBACK_TIMEOUT_MS = 300_000;

/** A fixed heading and detail line for the browser tab. */
export type LandingPage = [heading: string, detail: string];

/** The browser tab's copy: after a code, an `error` callback, or a callback without a code. */
export interface LoopbackPages {
  received: LandingPage;
  failed: LandingPage;
  incomplete: LandingPage;
}

export interface LoopbackAuthorizationOptions {
  hostname: string;
  callbackPath: string;
  redirectUri: (port: number) => string;
  authorizeUrl: (redirectUri: string, state: string) => string;
  openExternal: (url: string) => Promise<unknown>;
  pages: LoopbackPages;
  signal?: AbortSignal | undefined;
  timeoutMs?: number;
  onOpened?: () => void;
}

/** The accepted callback's query parameters and the redirect URI it arrived on. */
export interface LoopbackCallback {
  params: URLSearchParams;
  redirectUri: string;
}

export interface Pkce {
  verifier: string;
  challenge: string;
}

export function base64url(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A PKCE (S256) verifier and its challenge. */
export function makePkce(): Pkce {
  const verifier = base64url(crypto.randomBytes(64));
  const challenge = base64url(crypto.createHash("sha256").update(verifier).digest());
  return { verifier, challenge };
}

export const escapeHtml = (text: unknown): string =>
  String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const page = ([heading, detail]: LandingPage) =>
  '<html><body style="font-family:system-ui;text-align:center;padding:60px">' +
  `<h2>${escapeHtml(heading)}</h2><p>${escapeHtml(detail)}</p></body></html>`;

/**
 * Open the system browser at an authorize URL and wait for its redirect back
 * to a one-shot loopback listener.
 *
 * Resolves with the callback's query parameters once a request carrying the
 * expected `state` and a `code` arrives. Rejects on an `error` callback (the
 * error carries `errorCode` / `errorDescription`), a callback without a code,
 * the timeout (`code: "LOOPBACK_TIMEOUT"`), a browser that can't be opened
 * (`code: "BROWSER_UNAVAILABLE"`), or `signal` aborting.
 *
 * `hostname` is the interface to listen on (`localhost` or `127.0.0.1`);
 * `callbackPath` the only path the listener answers.
 */
export async function runLoopbackAuthorization({
  hostname,
  callbackPath,
  redirectUri: redirectUriFor,
  authorizeUrl,
  openExternal,
  pages,
  signal,
  timeoutMs = LOOPBACK_TIMEOUT_MS,
  onOpened,
}: LoopbackAuthorizationOptions): Promise<LoopbackCallback> {
  signal?.throwIfAborted();
  const state = base64url(crypto.randomBytes(24));
  let redirectUri: string;

  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      let reqUrl;
      try {
        reqUrl = new URL(String(req.url), "http://localhost");
      } catch {
        reqUrl = null;
      }
      if (!reqUrl || reqUrl.pathname !== callbackPath) {
        res.writeHead(404);
        res.end();
        return;
      }
      const params = reqUrl.searchParams;
      // An old browser tab must not terminate a new login on a reused loopback port.
      if (params.get("state") !== state) {
        res.writeHead(400, { "Content-Type": "text/plain" });
        res.end("This callback does not match the current sign-in.");
        return;
      }
      // Validate before answering: the desktop still has to exchange the code and
      // create the session, so this page must not claim sign-in succeeded.
      const fail = (error: Error, copy: LandingPage) => {
        res.writeHead(400, { "Content-Type": "text/html" });
        res.end(page(copy));
        cleanup();
        reject(error);
      };
      const err = params.get("error");
      if (err) {
        const desc = params.get("error_description");
        fail(
          Object.assign(new Error(`authorization error: ${err}${desc ? ` - ${desc}` : ""}`), {
            errorCode: err,
            errorDescription: desc ?? undefined,
          }),
          pages.failed,
        );
        return;
      }
      if (!params.get("code")) {
        fail(new Error("no code in callback"), pages.incomplete);
        return;
      }
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(page(pages.received));
      cleanup();
      resolve({ params, redirectUri });
    });

    const timer = setTimeout(() => {
      cleanup();
      reject(
        Object.assign(new Error("timed out waiting for browser login"), {
          code: "LOOPBACK_TIMEOUT",
        }),
      );
    }, timeoutMs);

    function cleanup() {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      server.close();
    }

    function onAbort() {
      cleanup();
      reject(signal?.reason);
    }

    server.on("error", (e) => {
      cleanup();
      reject(e);
    });

    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) {
      onAbort();
      return;
    }
    // Port 0: the OS picks a free port, so there is nothing to reserve and no
    // cross-app collision.
    server.listen(0, hostname, () => {
      if (signal?.aborted) {
        server.close();
        return;
      }
      redirectUri = redirectUriFor((server.address() as AddressInfo).port);
      openExternal(authorizeUrl(redirectUri, state)).then(
        () => onOpened?.(),
        (e: unknown) => {
          // Can't hand off to the browser — fail fast instead of waiting out the
          // auth timeout with a window the user can't complete.
          cleanup();
          reject(
            Object.assign(new Error(`could not open the system browser: ${(e as Error).message}`), {
              code: "BROWSER_UNAVAILABLE",
            }),
          );
        },
      );
    });
  });
}
