// Unit tests for the OIDC window session lifecycle (src/oidc-auth.ts), run with
// `node --test`. The cookie jar, window, and credentials are in-memory fakes;
// /v1/me is a fetch stub that accepts exactly the tokens the test allows.

import { describe, it, mock, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";

import { diagnostics } from "../src/diagnostics.ts";
import type { MintedSession } from "../src/oidc-credentials.ts";
import {
  createOidcAuth,
  oidcAuthRoute,
  type RenewalTimer,
  type SessionCookie,
  type SessionCookieDetails,
} from "../src/oidc-auth.ts";

const SERVER = "https://omni.example";
const COOKIE = "__Host-ap_session";
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

/** A rejection, with the `code` the module attaches. */
interface CodedError extends Error {
  code?: string;
}

/** The session cookie the fake jar is handed; tests may omit what they don't check. */
type CookieInput = Pick<SessionCookieDetails, "url" | "name" | "value"> &
  Partial<SessionCookieDetails>;

/** The `Cookie` header the module sends to /v1/me. */
function cookieHeader(init: RequestInit): string {
  return (init.headers as Record<string, string>).Cookie ?? "";
}

beforeEach(() => {
  mock.method(console, "log", () => {});
  mock.method(console, "warn", () => {});
});
afterEach(() => mock.restoreAll());

function fakeSession() {
  const jar = new Map<string, SessionCookie>();
  const cookies = Object.assign(new EventEmitter(), {
    jar,
    calls: [] as CookieInput[],
    async get({ name }: { name: string }) {
      const cookie = jar.get(name);
      return cookie ? [cookie] : [];
    },
    async set(details: CookieInput) {
      cookies.calls.push(details);
      if (jar.has(details.name))
        cookies.emit("changed", {}, jar.get(details.name), "overwrite", true);
      const cookie = {
        name: details.name,
        value: details.value,
        domain: new URL(details.url).hostname,
        expirationDate: details.expirationDate,
      };
      jar.set(details.name, cookie);
      cookies.emit("changed", {}, cookie, "explicit", false);
    },
    async remove(_url: string, name: string) {
      const cookie = jar.get(name);
      jar.delete(name);
      if (cookie) cookies.emit("changed", {}, cookie, "explicit", true);
    },
  });
  return { cookies };
}

interface FakeWindow {
  webContents: EventEmitter;
  loads: string[];
  destroyed: boolean;
  isDestroyed: () => boolean;
  loadURL: (url: string) => Promise<void>;
}

function fakeWindow(): FakeWindow {
  const webContents = new EventEmitter();
  const win: FakeWindow = {
    webContents,
    loads: [],
    destroyed: false,
    isDestroyed: () => win.destroyed,
    loadURL: async (url) => {
      win.loads.push(url);
    },
  };
  return win;
}

/** Fire a navigation event and report whether a listener prevented it. */
function navigate(win: FakeWindow, eventName: string, url: string, ...rest: boolean[]) {
  let prevented = false;
  win.webContents.emit(eventName, { preventDefault: () => (prevented = true) }, url, ...rest);
  return prevented;
}

interface HarnessOptions {
  refresh?: () => Promise<MintedSession>;
  signIn?: () => Promise<MintedSession>;
  accepted?: Set<string>;
}

interface FakeTimer {
  fn: () => void;
  delay: number;
  cleared?: boolean;
}

function harness({ refresh, signIn, accepted = new Set(["valid"]) }: HarnessOptions = {}) {
  const session = fakeSession();
  const credentials = {
    refreshSession: mock.fn(refresh ?? (async () => ({ token: "valid", expiresIn: 3600 }))),
    signInWithBrowser: mock.fn(signIn ?? (async () => ({ token: "valid", expiresIn: 28800 }))),
    signOut: mock.fn(async () => {}),
  };
  const origins = new Map<FakeWindow, string>();
  const authRequired: { win: FakeWindow; serverUrl: string; error: unknown }[] = [];
  const signedOut: { win: FakeWindow; serverUrl: string; complete: boolean }[] = [];
  const timers: FakeTimer[] = [];
  const fetchFn = mock.fn(async (url: string, init: RequestInit) => {
    assert.equal(url, `${SERVER}/v1/me`);
    const token = cookieHeader(init).split("=")[1] ?? "";
    return { status: accepted.has(token) ? 200 : 401 };
  });
  const auth = createOidcAuth<FakeWindow>({
    session,
    credentials,
    getOrigin: (win) => origins.get(win) ?? null,
    onAuthRequired: (win, serverUrl, error) => authRequired.push({ win, serverUrl, error }),
    onSignedOut: (win, serverUrl, details) => signedOut.push({ win, serverUrl, ...details }),
    fetchFn,
    // Timer handles are 1-based indexes into `timers`.
    setTimeoutFn: (fn, delay) => timers.push({ fn, delay }),
    clearTimeoutFn: (id: RenewalTimer | undefined) => {
      const timer = typeof id === "number" ? timers[id - 1] : undefined;
      if (timer) timer.cleared = true;
    },
  });
  const connect = (win = fakeWindow(), loadUrl = `${SERVER}/c/1`) => {
    origins.set(win, SERVER);
    auth.attach(win, { serverUrl: SERVER, cookieName: COOKIE, loadUrl });
    return win;
  };
  return { session, credentials, auth, authRequired, signedOut, timers, fetchFn, origins, connect };
}

describe("oidcAuthRoute", () => {
  it("matches the server's login and logout routes under its mount", () => {
    assert.equal(oidcAuthRoute(`${SERVER}/auth/login?return_to=/c/1`, SERVER), "login");
    assert.equal(oidcAuthRoute(`${SERVER}/auth/logout`, `${SERVER}/`), "logout");
    assert.equal(oidcAuthRoute(`${SERVER}/omni/auth/login`, `${SERVER}/omni/`), "login");
  });

  it("ignores other paths, other mounts, and other origins", () => {
    assert.equal(oidcAuthRoute(`${SERVER}/auth/callback`, SERVER), null);
    assert.equal(oidcAuthRoute(`${SERVER}/auth/login`, `${SERVER}/omni/`), null);
    assert.equal(oidcAuthRoute("https://idp.example/auth/login", SERVER), null);
    assert.equal(oidcAuthRoute("not a url", SERVER), null);
  });
});

describe("ensureSession", () => {
  it("keeps a cookie the server still accepts", async () => {
    const h = harness();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    assert.equal(await h.auth.ensureSession(SERVER, COOKIE, { interactive: true }), "existing");
    assert.equal(h.credentials.refreshSession.mock.callCount(), 0);
    assert.equal(h.credentials.signInWithBrowser.mock.callCount(), 0);
  });

  it("renews a rejected cookie from the stored grant without a browser", async () => {
    const h = harness();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "stale" });
    assert.equal(await h.auth.ensureSession(SERVER, COOKIE), "renewed");
    assert.equal(h.session.cookies.jar.get(COOKIE)?.value, "valid");
    assert.equal(h.credentials.signInWithBrowser.mock.callCount(), 0);
  });

  it("never opens the browser for a restore", async () => {
    const noGrant = Object.assign(new Error("none"), { code: "NO_STORED_TOKEN" });
    const h = harness({ refresh: async () => Promise.reject(noGrant) });
    await assert.rejects(h.auth.ensureSession(SERVER, COOKIE), (e: unknown) => e === noGrant);
    assert.equal(h.credentials.signInWithBrowser.mock.callCount(), 0);
  });

  it("signs in through the browser on Connect and installs the session cookie", async () => {
    const recorded: unknown[][] = [];
    mock.method(diagnostics, "record", (...args: unknown[]) => recorded.push(args));
    const noGrant = Object.assign(new Error("none"), { code: "NO_STORED_TOKEN" });
    const h = harness({ refresh: async () => Promise.reject(noGrant) });
    let announced = 0;
    const outcome = await h.auth.ensureSession(SERVER, COOKIE, {
      interactive: true,
      onBrowserSignIn: () => announced++,
    });
    assert.equal(outcome, "signed-in");
    assert.equal(announced, 1);
    const [details] = h.session.cookies.calls;
    assert.ok(details);
    assert.equal(details.url, SERVER);
    assert.equal(details.name, COOKIE);
    assert.equal(details.value, "valid");
    assert.equal(details.httpOnly, true);
    assert.equal(details.secure, true);
    assert.equal(details.path, "/");
    assert.ok((details.expirationDate ?? 0) > Date.now() / 1000 + 28000);
    assert.deepEqual(recorded, [
      ["oidc_session", { origin: SERVER, outcome: "browser" }],
      ["oidc_session", { origin: SERVER, outcome: "signed_in" }],
    ]);
  });

  it("does not open the browser when the server is unreachable", async () => {
    const offline = Object.assign(new Error("offline"), { code: "network" });
    const h = harness({ refresh: async () => Promise.reject(offline) });
    await assert.rejects(
      h.auth.ensureSession(SERVER, COOKIE, { interactive: true }),
      (e: unknown) => e === offline,
    );
    assert.equal(h.credentials.signInWithBrowser.mock.callCount(), 0);
  });

  it("fails when the server rejects the new session", async () => {
    const noGrant = Object.assign(new Error("none"), { code: "NO_STORED_TOKEN" });
    const h = harness({
      refresh: async () => Promise.reject(noGrant),
      signIn: async () => ({ token: "unknown", expiresIn: 60 }),
    });
    await assert.rejects(
      h.auth.ensureSession(SERVER, COOKIE, { interactive: true }),
      (e: CodedError) => e.code === "SESSION_REJECTED",
    );
  });
});

describe("session verification", () => {
  it("treats a proxy redirect or a refusal as a rejected session, not an outage", async () => {
    for (const status of [302, 403]) {
      const noGrant = Object.assign(new Error("none"), { code: "NO_STORED_TOKEN" });
      const h = harness({ refresh: async () => Promise.reject(noGrant) });
      h.fetchFn.mock.mockImplementation(async (_url, init) => ({
        status: cookieHeader(init).endsWith("=valid") ? 200 : status,
      }));
      // oxlint-disable-next-line no-await-in-loop -- one case at a time.
      await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "stale" });
      // oxlint-disable-next-line no-await-in-loop -- one case at a time.
      assert.equal(await h.auth.ensureSession(SERVER, COOKIE, { interactive: true }), "signed-in");
    }
  });

  it("never stores a new session the server rejects", async () => {
    const noGrant = Object.assign(new Error("none"), { code: "NO_STORED_TOKEN" });
    const h = harness({
      refresh: async () => Promise.reject(noGrant),
      signIn: async () => ({ token: "unknown", expiresIn: 60 }),
    });
    await assert.rejects(h.auth.ensureSession(SERVER, COOKIE, { interactive: true }));
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
  });

  it("never stores a renewed session the server rejects", async () => {
    const h = harness({ refresh: async () => ({ token: "unknown", expiresIn: 60 }) });
    await assert.rejects(
      h.auth.ensureSession(SERVER, COOKIE),
      (e: CodedError) => e.code === "SESSION_REJECTED",
    );
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
  });
});

describe("sign-out robustness", () => {
  it("reaches the connect screen even when forgetting the grant throws", async () => {
    const h = harness();
    h.credentials.signOut.mock.mockImplementation(() => {
      throw new Error("disk full");
    });
    const win = h.connect();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    assert.equal(await h.auth.signOutWindow(win), false);
    assert.equal(h.signedOut.length, 1);
    assert.equal(h.signedOut[0]?.complete, false, "the connect screen must not claim success");
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
  });
});

describe("window lifecycle", () => {
  it("blocks the SPA's sign-in redirect, renews, and returns to the last page", async () => {
    const h = harness();
    const win = h.connect();
    win.webContents.emit("did-navigate", {}, `${SERVER}/c/2`);
    assert.equal(navigate(win, "will-navigate", `${SERVER}/auth/login?return_to=/c/2`), true);
    await tick();
    await tick();
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
    assert.deepEqual(win.loads, [`${SERVER}/c/2`]);
    assert.equal(h.authRequired.length, 0);
  });

  it("blocks a server redirect to the sign-in route too", async () => {
    const h = harness();
    const win = h.connect();
    assert.equal(navigate(win, "will-redirect", `${SERVER}/auth/login`, false, true), true);
    // A subframe redirect is not the window's navigation.
    assert.equal(navigate(win, "will-redirect", `${SERVER}/auth/login`, false, false), false);
  });

  it("leaves other navigations alone", () => {
    const h = harness();
    const win = h.connect();
    assert.equal(navigate(win, "will-navigate", `${SERVER}/c/3`), false);
    assert.equal(navigate(win, "will-navigate", "https://docs.example/"), false);
  });

  it("returns to the connect screen when renewal fails after a sign-in request", async () => {
    const dead = Object.assign(new Error("dead"), { code: "invalid_grant" });
    const h = harness({ refresh: async () => Promise.reject(dead) });
    const win = h.connect();
    navigate(win, "will-navigate", `${SERVER}/auth/login`);
    await tick();
    await tick();
    assert.equal(h.authRequired.length, 1);
    assert.equal(h.authRequired[0]?.error, dead);
    assert.deepEqual(win.loads, []);
    assert.equal(h.auth.isAttached(win), false);
  });

  it("stops renewing when the server keeps rejecting the session", async () => {
    const h = harness();
    const win = h.connect();
    navigate(win, "will-navigate", `${SERVER}/auth/login`);
    await tick();
    await tick();
    navigate(win, "will-navigate", `${SERVER}/auth/login`);
    assert.equal(h.authRequired.length, 1);
    assert.equal((h.authRequired[0]?.error as CodedError | undefined)?.code, "SESSION_REJECTED");
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
  });

  it("reloads a page that asked to sign in during a background renewal", async () => {
    let release = () => {};
    const h = harness({
      refresh: () =>
        new Promise<MintedSession>((resolve) => {
          release = () => resolve({ token: "valid", expiresIn: 3600 });
        }),
    });
    const win = h.connect();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "x" });
    await h.session.cookies.remove(SERVER, COOKIE);
    navigate(win, "will-navigate", `${SERVER}/auth/login`);
    release();
    await tick();
    await tick();
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
    assert.deepEqual(win.loads, [`${SERVER}/c/1`]);
  });

  it("renews in the background when the cookie is removed, without reloading", async () => {
    const h = harness();
    const win = h.connect();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    await h.session.cookies.remove(SERVER, COOKIE);
    await tick();
    await tick();
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
    assert.equal(h.session.cookies.jar.get(COOKIE)?.value, "valid");
    assert.deepEqual(win.loads, []);
  });

  it("does not eject the window when a background renewal fails", async () => {
    const offline = Object.assign(new Error("offline"), { code: "network" });
    const h = harness({ refresh: async () => Promise.reject(offline) });
    h.connect();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    await h.session.cookies.remove(SERVER, COOKIE);
    await tick();
    await tick();
    assert.equal(h.authRequired.length, 0);
    // An unreachable server is retried later.
    assert.ok(h.timers.some((t) => t.delay === 30_000 && !t.cleared));
  });

  it("schedules renewal ahead of the cookie's expiry", async () => {
    const h = harness();
    await h.session.cookies.set({
      url: SERVER,
      name: COOKIE,
      value: "valid",
      expirationDate: Date.now() / 1000 + 3600,
    });
    h.connect();
    await tick();
    const timer = h.timers.at(-1);
    assert.ok(timer);
    assert.ok(timer.delay > 3_400_000 && timer.delay < 3_600_000, `delay ${timer.delay}`);
    timer.fn();
    await tick();
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
  });

  it("signs every window on the server out, without renewing", async () => {
    const h = harness();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    const first = h.connect();
    const second = h.connect(fakeWindow());
    assert.equal(navigate(first, "will-navigate", `${SERVER}/auth/logout`), true);
    await tick();
    await tick();
    assert.equal(h.credentials.signOut.mock.callCount(), 1);
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
    assert.deepEqual(
      h.signedOut.map((entry) => entry.win),
      [first, second],
    );
    assert.equal(h.credentials.refreshSession.mock.callCount(), 0);
    assert.equal(h.auth.isAttached(first), false);
  });

  it("keeps a renewal that finishes after sign-out from signing back in", async () => {
    let release = () => {};
    const h = harness({
      refresh: () =>
        new Promise<MintedSession>((resolve) => {
          release = () => resolve({ token: "valid", expiresIn: 3600 });
        }),
    });
    const win = h.connect();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    await h.session.cookies.remove(SERVER, COOKIE);
    assert.equal(h.credentials.refreshSession.mock.callCount(), 1);
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    navigate(win, "will-navigate", `${SERVER}/auth/logout`);
    await tick();
    release();
    await tick();
    await tick();
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
    assert.equal(h.signedOut.length, 1);
    assert.equal(h.authRequired.length, 0);
  });

  it("signs out on request, like the app's own sign-out", async () => {
    const h = harness();
    await h.session.cookies.set({ url: SERVER, name: COOKIE, value: "valid" });
    const win = h.connect();
    assert.equal(await h.auth.signOutWindow(win), true);
    assert.equal(h.credentials.signOut.mock.callCount(), 1);
    assert.equal(h.session.cookies.jar.has(COOKIE), false);
    assert.equal(h.signedOut.length, 1);
    assert.equal(await h.auth.signOutWindow(win), false, "a signed-out window has nothing left");
  });

  it("ignores a window that moved to another server", async () => {
    const h = harness();
    const win = h.connect();
    h.origins.set(win, "https://other.example");
    assert.equal(navigate(win, "will-navigate", `${SERVER}/auth/login`), false);
  });
});
