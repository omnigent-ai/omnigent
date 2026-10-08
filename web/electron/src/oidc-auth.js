// Session lifecycle for windows on an OIDC server: install and renew the session
// cookie, and turn the SPA's /auth/login and /auth/logout into a silent renewal
// or a sign-out so the identity provider never loads inside the app.

"use strict";

const { serverEndpoint } = require("./oidc-credentials");

// The SPA asking to sign in again this soon after a renewal means the server
// rejected the renewed session: stop instead of renewing in a loop.
const REJECTED_RENEWAL_WINDOW_MS = 15_000;
const VERIFY_TIMEOUT_MS = 10_000;
// A background renewal that couldn't reach the server tries again after this.
const RETRY_DELAY_MS = 30_000;

/**
 * Which shell-handled auth route a main-frame URL is, if any: the server's
 * `/auth/login` or `/auth/logout` under its mount.
 *
 * @param {string} rawUrl
 * @param {string} serverUrl
 * @returns {"login" | "logout" | null}
 */
function oidcAuthRoute(rawUrl, serverUrl) {
  let url;
  let base;
  try {
    url = new URL(rawUrl);
    base = new URL(serverUrl);
  } catch {
    return null;
  }
  if (url.origin !== base.origin) return null;
  const mount = base.pathname.replace(/\/+$/, "");
  if (url.pathname === `${mount}/auth/login`) return "login";
  if (url.pathname === `${mount}/auth/logout`) return "logout";
  return null;
}

function cookieMatches(cookie, origin, name) {
  const host = new URL(origin).hostname;
  return cookie.name === name && (cookie.domain ?? "").replace(/^\./, "") === host;
}

/**
 * @param {{
 *   session: Electron.Session,
 *   credentials: {
 *     signInWithBrowser: (serverUrl: string, options: { signal?: AbortSignal }) =>
 *       Promise<{ token: string, expiresIn: number | null }>,
 *     refreshSession: (serverUrl: string) =>
 *       Promise<{ token: string, expiresIn: number | null }>,
 *     signOut: (serverUrl: string) => Promise<void>,
 *   },
 *   getOrigin: (win: Electron.BrowserWindow) => string | null,
 *   onAuthRequired: (win: Electron.BrowserWindow, serverUrl: string, error: Error) => void,
 *   onSignedOut: (win: Electron.BrowserWindow, serverUrl: string) => void,
 *   fetchFn?: typeof fetch,
 *   now?: () => number,
 *   setTimeoutFn?: typeof setTimeout,
 *   clearTimeoutFn?: typeof clearTimeout,
 * }} options
 */
function createOidcAuth({
  session,
  credentials,
  getOrigin,
  onAuthRequired,
  onSignedOut,
  fetchFn = (...args) => fetch(...args),
  now = Date.now,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  const connections = new Map();
  const renewals = new Map();
  // Bumped per origin on sign-out, so a renewal already in flight can't
  // reinstall a session the user just signed out of.
  const signOutGenerations = new Map();

  async function installSession(serverUrl, cookieName, { token, expiresIn }) {
    const origin = new URL(serverUrl).origin;
    const details = {
      url: origin,
      name: cookieName,
      value: token,
      httpOnly: true,
      secure: origin.startsWith("https:"),
      sameSite: "lax",
      path: "/",
    };
    // A lifetime keeps the session across restarts, as the server's own cookie does.
    if (expiresIn) details.expirationDate = Math.floor(now() / 1000) + expiresIn;
    try {
      await session.cookies.set(details);
    } catch (error) {
      throw Object.assign(new Error(`could not store the ${cookieName} cookie`), {
        code: "SESSION_REJECTED",
        cause: error,
      });
    }
  }

  /** Whether the server accepts `token` as its session cookie (GET /v1/me → 200). */
  async function tokenAccepted(serverUrl, cookieName, token, signal) {
    const timeout = AbortSignal.timeout(VERIFY_TIMEOUT_MS);
    let response;
    try {
      response = await fetchFn(serverEndpoint(serverUrl, "/v1/me"), {
        headers: { Cookie: `${cookieName}=${token}` },
        redirect: "manual",
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch (error) {
      signal?.throwIfAborted();
      throw Object.assign(new Error(`could not reach ${new URL(serverUrl).host}`), {
        code: "network",
        cause: error,
      });
    }
    if (response.status === 200) return true;
    // A refusal, or a proxy redirecting to its own sign-in: not a valid session.
    if (response.status === 401 || response.status === 403) return false;
    if (response.status >= 300 && response.status < 400) return false;
    throw Object.assign(new Error(`GET /v1/me returned HTTP ${response.status}`), {
      code: "network",
    });
  }

  async function storedToken(serverUrl, cookieName) {
    const origin = new URL(serverUrl).origin;
    const cookies = await session.cookies.get({ url: origin, name: cookieName });
    return cookies.find((cookie) => cookieMatches(cookie, origin, cookieName))?.value ?? null;
  }

  /** Mint from the refresh grant (one request per origin) and install it. */
  function renewSession(serverUrl, cookieName) {
    const origin = new URL(serverUrl).origin;
    const existing = renewals.get(origin);
    if (existing) return existing;
    const generation = signOutGenerations.get(origin) ?? 0;
    const pending = credentials
      .refreshSession(serverUrl)
      .then(async (minted) => {
        // Only a session the server accepts goes into the jar.
        if (!(await tokenAccepted(serverUrl, cookieName, minted.token))) {
          throw Object.assign(new Error("the server rejected the renewed session"), {
            code: "SESSION_REJECTED",
          });
        }
        if ((signOutGenerations.get(origin) ?? 0) !== generation) {
          throw Object.assign(new Error("signed out during renewal"), { code: "SIGNED_OUT" });
        }
        return installSession(serverUrl, cookieName, minted);
      })
      .finally(() => {
        if (renewals.get(origin) === pending) renewals.delete(origin);
      });
    renewals.set(origin, pending);
    return pending;
  }

  /**
   * Renew from the stored grant during a connect. Returns null once the server
   * accepts the renewed cookie, else the error explaining why not. Cancellation
   * and an unreachable server are thrown: neither is a reason to sign in again.
   */
  async function renewForConnect(serverUrl, cookieName, signal) {
    try {
      await renewSession(serverUrl, cookieName);
      signal?.throwIfAborted();
      return null;
    } catch (error) {
      if (error?.name === "AbortError" || signal?.aborted || error?.code === "network") {
        throw error;
      }
      return error instanceof Error ? error : new Error(String(error));
    }
  }

  /**
   * Make sure the session cookie for `serverUrl` is one the server accepts.
   *
   * A valid cookie is kept; otherwise the stored refresh grant renews it. Only
   * an `interactive` call (the user chose Connect) goes on to sign in through
   * the system browser; restores and renewals throw instead.
   *
   * @param {string} serverUrl
   * @param {string} cookieName
   * @param {{ interactive?: boolean, signal?: AbortSignal, onBrowserSignIn?: () => void }} [options]
   * @returns {Promise<"existing" | "renewed" | "signed-in">}
   */
  async function ensureSession(
    serverUrl,
    cookieName,
    { interactive = false, signal, onBrowserSignIn } = {},
  ) {
    const existing = await storedToken(serverUrl, cookieName);
    signal?.throwIfAborted();
    if (existing && (await tokenAccepted(serverUrl, cookieName, existing, signal))) {
      return "existing";
    }
    const renewError = await renewForConnect(serverUrl, cookieName, signal);
    if (!renewError) return "renewed";
    if (!interactive) throw renewError;
    onBrowserSignIn?.();
    const minted = await credentials.signInWithBrowser(serverUrl, { signal });
    signal?.throwIfAborted();
    if (!(await tokenAccepted(serverUrl, cookieName, minted.token, signal))) {
      throw Object.assign(new Error("the server rejected the new session"), {
        code: "SESSION_REJECTED",
      });
    }
    await installSession(serverUrl, cookieName, minted);
    return "signed-in";
  }

  function current(ctx) {
    return (
      connections.get(ctx.win) === ctx &&
      !ctx.win.isDestroyed() &&
      getOrigin(ctx.win) === ctx.origin
    );
  }

  function detach(win) {
    const ctx = connections.get(win);
    if (!ctx) return;
    connections.delete(win);
    clearTimeoutFn(ctx.timer);
    ctx.webContents.removeListener("did-navigate", ctx.onNavigate);
    ctx.webContents.removeListener("did-navigate-in-page", ctx.onNavigateInPage);
    ctx.webContents.removeListener("will-navigate", ctx.onWillNavigate);
    ctx.webContents.removeListener("will-redirect", ctx.onWillRedirect);
  }

  function fail(ctx, error) {
    if (!current(ctx)) return;
    detach(ctx.win);
    onAuthRequired(ctx.win, ctx.serverUrl, error);
  }

  async function schedule(ctx) {
    const generation = ++ctx.scheduleGeneration;
    clearTimeoutFn(ctx.timer);
    const cookies = await session.cookies.get({ url: ctx.origin, name: ctx.cookieName });
    if (!current(ctx) || generation !== ctx.scheduleGeneration) return;
    const expiries = cookies
      .filter((cookie) => cookieMatches(cookie, ctx.origin, ctx.cookieName))
      .map((cookie) => cookie.expirationDate * 1000)
      .filter(Number.isFinite);
    // No local expiry (or no cookie yet): the SPA's own sign-in redirect recovers.
    if (!expiries.length) return;
    const remaining = Math.min(...expiries) - now();
    const delay = Math.max(
      1000,
      Math.min(2_147_483_647, remaining - Math.min(60_000, remaining / 5)),
    );
    ctx.timer = setTimeoutFn(() => void renew(ctx, { reason: "cookie expiry" }), delay);
    ctx.timer?.unref?.();
  }

  /**
   * Renew the window's session from the refresh grant. `reload` means the SPA
   * asked to sign in again and is now stalled on a blocked navigation: the
   * window reloads once renewed, and a failure returns it to the connect
   * screen. Background renewals (timer, cookie removal) never do either: the
   * current cookie may still be valid, and an unreachable server is retried.
   */
  function renew(ctx, { reload = false, reason = "renewal" } = {}) {
    if (!current(ctx)) return Promise.resolve();
    if (reload) ctx.reloadRequested = true;
    if (ctx.pending) return ctx.pending;
    clearTimeoutFn(ctx.timer);
    console.log("[omnigent] oidc auth: silent renewal", { origin: ctx.origin, reason, reload });
    ctx.pending = renewSession(ctx.serverUrl, ctx.cookieName)
      .then(async () => {
        if (!current(ctx)) return;
        await schedule(ctx);
        if (!ctx.reloadRequested || !current(ctx)) return;
        ctx.reloadRequested = false;
        try {
          await ctx.win.loadURL(ctx.returnUrl);
        } catch (error) {
          // A superseding navigation or a load failure is the window's own
          // did-fail-load path to report, not a sign-in problem.
          console.warn("[omnigent] oidc auth: recovery reload did not land", {
            origin: ctx.origin,
            code: error.code,
          });
        }
      })
      .catch((error) => {
        if (ctx.reloadRequested) {
          fail(ctx, error);
          return;
        }
        console.warn("[omnigent] oidc auth: background renewal failed", {
          origin: ctx.origin,
          code: error.code,
        });
        if (error.code === "network" && current(ctx)) {
          ctx.timer = setTimeoutFn(() => void renew(ctx, { reason: "retry" }), RETRY_DELAY_MS);
          ctx.timer?.unref?.();
        }
      })
      .finally(() => {
        ctx.pending = null;
        // A sign-in request that landed after the outcome was handled still
        // has a stalled page waiting on it.
        if (ctx.reloadRequested && current(ctx)) {
          ctx.reloadRequested = false;
          void ctx.win.loadURL(ctx.returnUrl).catch(() => {});
        }
      });
    return ctx.pending;
  }

  function recover(ctx) {
    if (!current(ctx)) return;
    if (!ctx.pending && now() - ctx.lastRecoveredAt < REJECTED_RENEWAL_WINDOW_MS) {
      fail(
        ctx,
        Object.assign(new Error("the server rejected the renewed session"), {
          code: "SESSION_REJECTED",
        }),
      );
      return;
    }
    ctx.lastRecoveredAt = now();
    void renew(ctx, { reload: true, reason: "sign-in requested" });
  }

  async function signOut(ctx) {
    const { serverUrl, origin, cookieName } = ctx;
    // Detach every window on this server first, so removing the shared cookie
    // reads as a sign-out rather than an expiry to renew.
    const signedOut = [...connections.values()].filter((c) => c.origin === origin && current(c));
    for (const c of signedOut) detach(c.win);
    signOutGenerations.set(origin, (signOutGenerations.get(origin) ?? 0) + 1);
    renewals.delete(origin);
    console.log("[omnigent] oidc auth: signing out", { origin, windows: signedOut.length });
    // Local sign-out must not wait on the network: the grant is forgotten
    // synchronously, the cookie removed, and revocation finishes after. The
    // windows reach the connect screen even if forgetting the grant fails.
    let revocation = Promise.resolve();
    let complete = true;
    try {
      revocation = credentials.signOut(serverUrl);
    } catch (error) {
      complete = false;
      console.warn("[omnigent] oidc auth: could not forget the grant", { origin, error });
    }
    try {
      await session.cookies.remove(origin, cookieName);
    } catch (error) {
      complete = false;
      console.warn("[omnigent] oidc auth: could not clear the session cookie", { origin, error });
    }
    for (const c of signedOut)
      if (!c.win.isDestroyed()) onSignedOut(c.win, serverUrl, { complete });
    await revocation.catch(() => {});
    return complete;
  }

  /**
   * Own the session lifecycle of a window that just loaded an OIDC server.
   *
   * @param {Electron.BrowserWindow} win
   * @param {{ serverUrl: string, cookieName: string, loadUrl?: string }} connection
   */
  function attach(win, { serverUrl, cookieName, loadUrl = serverUrl }) {
    detach(win);
    const origin = new URL(serverUrl).origin;
    const ctx = {
      win,
      webContents: win.webContents,
      serverUrl,
      origin,
      cookieName,
      returnUrl: loadUrl,
      timer: null,
      pending: null,
      scheduleGeneration: 0,
      lastRecoveredAt: -Infinity,
      reloadRequested: false,
    };
    const remember = (url) => {
      if (!current(ctx)) return;
      try {
        if (new URL(url).origin !== origin) return;
      } catch {
        return;
      }
      if (!oidcAuthRoute(url, serverUrl)) ctx.returnUrl = url;
    };
    const intercept = (event, url) => {
      if (!current(ctx)) return;
      const route = oidcAuthRoute(url, serverUrl);
      if (!route) return;
      // The IdP must never load inside the window: the shell renews or signs out.
      event.preventDefault();
      if (route === "logout") void signOut(ctx);
      else recover(ctx);
    };
    ctx.onNavigate = (_event, url) => remember(url);
    ctx.onNavigateInPage = (_event, url, isMainFrame) => {
      if (isMainFrame) remember(url);
    };
    ctx.onWillNavigate = (event, url) => intercept(event, url);
    ctx.onWillRedirect = (event, url, _isInPlace, isMainFrame) => {
      if (isMainFrame !== false) intercept(event, url);
    };
    connections.set(win, ctx);
    win.webContents.on("did-navigate", ctx.onNavigate);
    win.webContents.on("did-navigate-in-page", ctx.onNavigateInPage);
    win.webContents.on("will-navigate", ctx.onWillNavigate);
    win.webContents.on("will-redirect", ctx.onWillRedirect);
    void schedule(ctx);
  }

  const onCookieChanged = (_event, cookie, cause, removed) => {
    for (const ctx of connections.values()) {
      if (!current(ctx) || ctx.pending || !cookieMatches(cookie, ctx.origin, ctx.cookieName))
        continue;
      // Chromium removes the old cookie before inserting its replacement.
      if (removed && (cause === "overwrite" || cause === "expired-overwrite")) continue;
      if (removed) void renew(ctx, { reason: `cookie removed (${cause})` });
      else void schedule(ctx);
    }
  };
  session.cookies.on("changed", onCookieChanged);

  /**
   * Sign the window's server out, as the web app's own sign-out does: every
   * window on it returns to the connect screen.
   *
   * @returns {Promise<boolean>} false when the window has no OIDC session
   *   here, or the saved sign-in couldn't be fully cleared.
   */
  async function signOutWindow(win) {
    const ctx = connections.get(win);
    if (!ctx || !current(ctx)) return false;
    return signOut(ctx);
  }

  return {
    ensureSession,
    attach,
    detach,
    signOutWindow,
    isAttached: (win) => connections.has(win),
    dispose() {
      for (const win of [...connections.keys()]) detach(win);
      session.cookies.removeListener("changed", onCookieChanged);
    },
  };
}

module.exports = { oidcAuthRoute, createOidcAuth, REJECTED_RENEWAL_WINDOW_MS };
