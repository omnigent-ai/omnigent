// Databricks browser-auth lifecycle. Embedded SSO never participates in this mode.

import { isDatabricksOAuthServerUrl } from "./url.ts";

export const DATABRICKS_BROWSER_AUTH_KEY = "DatabricksBrowserAuthEnabled";

/** `browser` signs in to workspaces in the system browser; `embedded` keeps in-app SSO. */
export type DatabricksAuthMode = "browser" | "embedded";

/** The macOS user-defaults reads behind the auth mode (Electron's `systemPreferences`). */
export interface DatabricksAuthModeSource {
  platform?: NodeJS.Platform;
  registerDefaults?: ((defaults: Record<string, boolean>) => void) | undefined;
  getUserDefault?: ((key: string, type: "boolean") => unknown) | undefined;
}

export function readDatabricksAuthMode({
  platform = process.platform,
  registerDefaults,
  getUserDefault,
}: DatabricksAuthModeSource = {}): DatabricksAuthMode {
  if (platform !== "darwin" || !registerDefaults || !getUserDefault) return "browser";
  try {
    // NSUserDefaults otherwise returns false for both missing and explicitly false keys.
    registerDefaults({ [DATABRICKS_BROWSER_AUTH_KEY]: true });
    return getUserDefault(DATABRICKS_BROWSER_AUTH_KEY, "boolean") === false
      ? "embedded"
      : "browser";
  } catch {
    return "browser";
  }
}

export function usesDatabricksBrowserAuth(url: unknown, mode: DatabricksAuthMode): boolean {
  return mode === "browser" && isDatabricksOAuthServerUrl(url);
}

export function isDatabricksLoginUrl(rawUrl: string, origin: string): boolean {
  try {
    const url = new URL(rawUrl);
    const p = url.pathname.toLowerCase();
    return (
      url.origin === origin &&
      (p === "/login" ||
        p.startsWith("/login/") ||
        p === "/login.html" ||
        p.startsWith("/oidc/") ||
        p.startsWith("/.auth/"))
    );
  } catch {
    return false;
  }
}

/** A cookie as the session's cookie jar reports it. */
export interface WorkspaceCookie {
  name: string;
  domain?: string | undefined;
  hostOnly?: boolean | undefined;
  expirationDate?: number | undefined;
}

export function cookieMatchesOrigin(cookie: WorkspaceCookie, origin: string): boolean {
  const host = new URL(origin).hostname;
  const domain = (cookie.domain ?? "").replace(/^\./, "");
  return (
    cookie.name === "DBAUTH" &&
    (host === domain || (!cookie.hostOnly && host.endsWith(`.${domain}`)))
  );
}

// The workspace rejected a freshly minted session: the stored credentials need a browser sign-in.
export const SESSION_REJECTED = "SESSION_REJECTED";
// The workspace IP access list refused session-create (e.g. off the VPN): retried.
export const IP_ACL_BLOCKED = "IP_ACL_BLOCKED";
const sessionRejected = (message: string) =>
  Object.assign(new Error(message), { errorCode: SESSION_REJECTED });
// Session-create timed out or its response was cut off (databricks-session.ts).
export const SESSION_TRANSPORT = "SESSION_TRANSPORT";

/** Whether a renewal failure is worth retrying: no response, HTTP 5xx/429, or an IP ACL block. */
export function isTransientRenewalError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const fields = error as Record<string, unknown>;
  if (fields.errorCode === IP_ACL_BLOCKED || fields.errorCode === SESSION_TRANSPORT) return true;
  const { status } = fields;
  if (status != null) {
    // Relational comparison converts `status` to a number the same way.
    const numeric = Number(status);
    return status === 429 || (numeric >= 500 && numeric <= 599);
  }
  const message = typeof fields.message === "string" ? fields.message : "";
  return (
    fields.name === "TimeoutError" ||
    (fields.name === "TypeError" && (message === "fetch failed" || message === "terminated")) ||
    message.startsWith("net::ERR_")
  );
}

type CookieChangedListener = (
  event: unknown,
  cookie: WorkspaceCookie,
  cause: string,
  removed: boolean,
) => void;

/** The parts of a request's details the guard reads. */
export interface GuardedRequest {
  webContentsId?: number | undefined;
  url: string;
  resourceType: string;
}

export type BeforeRequestListener = (
  details: GuardedRequest,
  callback: (response: { cancel?: boolean }) => void,
) => void;

/** The part of Electron's `Session` the request guard and renewal use. */
export interface DatabricksAuthSession {
  cookies: {
    get: (filter: { url: string; name: string }) => Promise<WorkspaceCookie[]>;
    on: (event: "changed", listener: CookieChangedListener) => unknown;
    removeListener: (event: "changed", listener: CookieChangedListener) => unknown;
  };
  webRequest: {
    onBeforeRequest: (listener: BeforeRequestListener | null) => void;
  };
}

/** A navigation event; only `preventDefault` is used. */
export interface NavigationEvent {
  preventDefault: () => void;
}

type NavigateListener = (event: unknown, url: string) => void;
type NavigateInPageListener = (event: unknown, url: string, isMainFrame: boolean) => void;
type GuardListener = (event: NavigationEvent, url: string) => void;

/** The navigation events of a window's webContents this module observes. */
export interface DatabricksAuthWebContents {
  on: ((event: "did-navigate", listener: NavigateListener) => unknown) &
    ((event: "did-navigate-in-page", listener: NavigateInPageListener) => unknown) &
    ((event: "will-navigate" | "will-redirect", listener: GuardListener) => unknown);
  removeListener: ((event: "did-navigate", listener: NavigateListener) => unknown) &
    ((event: "did-navigate-in-page", listener: NavigateInPageListener) => unknown) &
    ((event: "will-navigate" | "will-redirect", listener: GuardListener) => unknown);
}

export interface DatabricksAuthWindow {
  webContents: DatabricksAuthWebContents;
  isDestroyed: () => boolean;
  loadURL: (url: string) => Promise<unknown>;
}

/** A timer handle from the real or an injected `setTimeout`. */
export type RenewalTimer = ReturnType<typeof setTimeout> | number;

export interface DatabricksAuthDeps<W extends DatabricksAuthWindow> {
  session: DatabricksAuthSession;
  ensureSession: (
    session: DatabricksAuthSession,
    origin: string,
    options: { interactive: false },
  ) => Promise<unknown>;
  getWindow: (webContentsId: number | undefined) => W | null | undefined;
  getOrigin: (win: W) => string | null;
  onAuthRequired: (
    win: W,
    serverUrl: string,
    error: unknown,
    details: { returnUrl: string },
  ) => void;
  isSetupUrl?: (url: string) => boolean;
  now?: () => number;
  setTimeoutFn?: (callback: () => void, delay: number) => RenewalTimer;
  clearTimeoutFn?: (timer: RenewalTimer | undefined) => void;
}

export interface DatabricksAuth<W extends DatabricksAuthWindow> {
  attach: (win: W, serverUrl: string, loadUrl?: string) => Promise<void>;
  detach: (win: W) => void;
  rejectConnection: (win: W) => void;
  reset: (win: W) => void;
  recover: (win: W) => void;
  whenSettled: (origin: string) => Promise<void>;
  dispose: () => void;
}

/** One attached window's renewal state. */
interface Connection<W extends DatabricksAuthWindow> {
  win: W;
  webContents: DatabricksAuthWebContents;
  origin: string;
  serverUrl: string;
  returnUrl: string;
  timer: RenewalTimer | undefined;
  pending: Promise<void> | null;
  scheduleGeneration: number;
  navigationVersion: number;
  reloadVersion: number | null;
  lastRejectedAt: number;
  onNavigate: NavigateListener;
  onNavigateInPage: NavigateInPageListener;
}

/** One session owns the request guard; each window owns its renewal lifecycle. */
export function createDatabricksAuth<W extends DatabricksAuthWindow>({
  session,
  ensureSession,
  getWindow,
  getOrigin,
  onAuthRequired,
  isSetupUrl = () => false,
  now = Date.now,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}: DatabricksAuthDeps<W>): DatabricksAuth<W> {
  const connections = new Map<W, Connection<W>>();
  const renewals = new Map<string, Promise<unknown>>();
  const rejectedWindows = new Map<
    W,
    { webContents: DatabricksAuthWebContents; guard: GuardListener }
  >();

  function current(ctx: Connection<W>): boolean {
    return (
      connections.get(ctx.win) === ctx &&
      !ctx.win.isDestroyed() &&
      getOrigin(ctx.win) === ctx.origin
    );
  }

  function detach(win: W) {
    const ctx = connections.get(win);
    if (!ctx) return;
    connections.delete(win);
    clearTimeoutFn(ctx.timer);
    ctx.webContents.removeListener("did-navigate", ctx.onNavigate);
    ctx.webContents.removeListener("did-navigate-in-page", ctx.onNavigateInPage);
  }

  function rejectConnection(win: W) {
    detach(win);
    if (win.isDestroyed() || rejectedWindows.has(win)) return;
    const webContents = win.webContents;
    const guard: GuardListener = (event, url) => {
      if (!isSetupUrl(url)) event.preventDefault();
    };
    rejectedWindows.set(win, { webContents, guard });
    // Stop renderer redirects before they can interrupt the selector's pending load.
    webContents.on("will-navigate", guard);
    webContents.on("will-redirect", guard);
  }

  function reset(win: W) {
    detach(win);
    const rejected = rejectedWindows.get(win);
    if (rejected) {
      rejected.webContents.removeListener("will-navigate", rejected.guard);
      rejected.webContents.removeListener("will-redirect", rejected.guard);
      rejectedWindows.delete(win);
    }
  }

  function fail(ctx: Connection<W>, error: unknown) {
    if (!current(ctx)) return;
    rejectConnection(ctx.win);
    onAuthRequired(ctx.win, ctx.serverUrl, error, { returnUrl: ctx.returnUrl });
  }

  async function schedule(ctx: Connection<W>): Promise<void> {
    const generation = ++ctx.scheduleGeneration;
    clearTimeoutFn(ctx.timer);
    const cookies = await session.cookies.get({ url: ctx.serverUrl, name: "DBAUTH" });
    if (!current(ctx) || generation !== ctx.scheduleGeneration) return;
    if (!cookies.length) throw new Error("Databricks session cookie is missing");
    const expiries = cookies
      .map((c) => (c.expirationDate ?? Number.NaN) * 1000)
      .filter(Number.isFinite);
    // Session cookies have no local expiry timestamp; removal and auth rejection still recover.
    if (!expiries.length) return;
    const remaining = Math.min(...expiries) - now();
    if (remaining <= 0) throw new Error("Databricks session cookie is expired");
    const delay = Math.max(
      1000,
      Math.min(2_147_483_647, remaining - Math.min(60_000, remaining / 5)),
    );
    ctx.timer = setTimeoutFn(() => {
      void renew(ctx, { reason: "cookie expiry" });
    }, delay);
    if (typeof ctx.timer === "object") ctx.timer.unref();
  }

  function renew(
    ctx: Connection<W>,
    { reload = false, reason = "cookie renewal" }: { reload?: boolean; reason?: string } = {},
  ): Promise<void> {
    if (!current(ctx)) return Promise.resolve();
    if (reload) ctx.reloadVersion = ctx.navigationVersion;
    if (ctx.pending) return ctx.pending;
    clearTimeoutFn(ctx.timer);
    console.log("[omnigent] databricks auth: silent renewal", {
      origin: ctx.origin,
      reason,
      shared: renewals.has(ctx.origin),
      reload,
    });
    const pending =
      renewals.get(ctx.origin) ??
      Promise.resolve().then(() => ensureSession(session, ctx.origin, { interactive: false }));
    renewals.set(ctx.origin, pending);
    ctx.pending = pending
      .then(async () => {
        if (!current(ctx)) return;
        await schedule(ctx);
        if (!current(ctx)) return;
        if (ctx.reloadVersion !== ctx.navigationVersion) return;
        // The cookie is renewed and verified by here, so how the reload itself
        // lands is not a credential signal. A newer navigation supersedes this
        // one with ERR_ABORTED, and a genuine load failure is the window's
        // did-fail-load fallback to report — neither means sign-in is required.
        try {
          await ctx.win.loadURL(ctx.returnUrl);
        } catch (error) {
          console.warn("[omnigent] databricks auth: recovery reload did not land", {
            origin: ctx.origin,
            code: (error as { code?: unknown }).code,
          });
        }
      })
      .catch((error) => fail(ctx, error))
      .finally(() => {
        if (renewals.get(ctx.origin) === pending) renewals.delete(ctx.origin);
        ctx.pending = null;
        ctx.reloadVersion = null;
      });
    return ctx.pending;
  }

  function recover(win: W) {
    const ctx = connections.get(win);
    if (!ctx || !current(ctx)) return;
    if (!ctx.pending && now() - ctx.lastRejectedAt < 15_000) {
      fail(
        ctx,
        sessionRejected("Databricks rejected the renewed session; sign in again in your browser"),
      );
      return;
    }
    ctx.lastRejectedAt = now();
    void renew(ctx, { reload: true, reason: "blocked workspace login" });
  }

  async function attach(win: W, serverUrl: string, loadUrl = serverUrl): Promise<void> {
    const origin = new URL(serverUrl).origin;
    if (!isDatabricksOAuthServerUrl(origin) || getOrigin(win) !== origin) {
      throw new Error("Browser session renewal requires a pinned Databricks workspace");
    }
    reset(win);
    const ctx: Connection<W> = {
      win,
      webContents: win.webContents,
      origin,
      serverUrl,
      returnUrl: loadUrl,
      timer: undefined,
      pending: null,
      scheduleGeneration: 0,
      navigationVersion: 0,
      reloadVersion: null,
      lastRejectedAt: -Infinity,
      onNavigate: (_event, url) => remember(url),
      onNavigateInPage: (_event, url, isMainFrame) => {
        if (isMainFrame) remember(url);
      },
    };
    const remember = (url: string) => {
      if (!current(ctx)) return;
      try {
        if (new URL(url).origin !== origin) return;
        if (isDatabricksLoginUrl(url, origin)) {
          fail(
            ctx,
            sessionRejected("Workspace authentication is required; sign in again in your browser"),
          );
          return;
        }
      } catch {
        return;
      }
      ctx.returnUrl = url;
      ctx.navigationVersion++;
    };
    connections.set(win, ctx);
    win.webContents.on("did-navigate", ctx.onNavigate);
    win.webContents.on("did-navigate-in-page", ctx.onNavigateInPage);
    try {
      await schedule(ctx);
    } catch (error) {
      if (connections.get(win) === ctx) detach(win);
      throw error;
    }
  }

  const onCookieChanged: CookieChangedListener = (_event, cookie, cause, removed) => {
    for (const ctx of connections.values()) {
      if (!current(ctx) || ctx.pending || !cookieMatchesOrigin(cookie, ctx.origin)) continue;
      // Chromium removes the old cookie before inserting its replacement.
      if (removed && (cause === "overwrite" || cause === "expired-overwrite")) continue;
      if (removed) void renew(ctx, { reason: `cookie removed (${cause})` });
      else void schedule(ctx).catch((error) => fail(ctx, error));
    }
  };
  session.cookies.on("changed", onCookieChanged);

  session.webRequest.onBeforeRequest((details, callback) => {
    const win = getWindow(details.webContentsId);
    // Unpinning revokes IPC trust, not this boundary: late redirects must not replace setup.
    if (win && rejectedWindows.has(win)) {
      const document = details.resourceType === "mainFrame" || details.resourceType === "subFrame";
      callback(document && !isSetupUrl(details.url) ? { cancel: true } : {});
      return;
    }
    const origin = win && getOrigin(win);
    if (!win || !origin) {
      callback({});
      return;
    }
    const login = isDatabricksLoginUrl(details.url, origin);
    let foreignDocument = false;
    try {
      foreignDocument =
        details.resourceType === "mainFrame" && new URL(details.url).origin !== origin;
    } catch {
      /* Invalid requests fail in Chromium. */
    }
    if (!login && !foreignDocument) {
      callback({});
      return;
    }
    callback({ cancel: true });
    if (login) recover(win);
    else {
      const ctx = connections.get(win);
      if (ctx)
        fail(
          ctx,
          new Error(
            "Workspace navigation left the authenticated origin; reconnect in your browser",
          ),
        );
    }
  });

  return {
    attach,
    detach,
    rejectConnection,
    reset,
    recover,
    /** Resolves once any renewal already running for `origin` has finished. */
    async whenSettled(origin) {
      await renewals.get(origin)?.catch(() => {});
    },
    dispose() {
      for (const win of connections.keys()) detach(win);
      for (const win of rejectedWindows.keys()) reset(win);
      session.cookies.removeListener("changed", onCookieChanged);
      session.webRequest.onBeforeRequest(null);
    },
  };
}
