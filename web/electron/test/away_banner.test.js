const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  AWAY_BANNER_DELAY_MS,
  isAuthGatePath,
  registerServerAwayWatch,
} = require("../src/away_banner");

const ORIGIN = "https://ws.cloud.databricks.com";
const FOREIGN = "https://company.okta.com";

class FakeWebContents extends EventEmitter {
  constructor(url = "") {
    super();
    this.url = url;
  }

  getURL() {
    return this.url;
  }
}

/** Deterministic timers: run() executes any pending callback. */
function fakeTimers() {
  const timers = new Map();
  let nextId = 0;
  return {
    setTimeoutFn: (fn, _ms) => {
      const id = ++nextId;
      timers.set(id, fn);
      return id;
    },
    clearTimeoutFn: (id) => {
      timers.delete(id);
    },
    pending: () => timers.size,
    run: () => {
      for (const fn of [...timers.values()]) fn();
      timers.clear();
    },
  };
}

function makeWatch({ pinned = ORIGIN, url = "", ...opts } = {}) {
  const webContents = new FakeWebContents(url);
  const timers = fakeTimers();
  const events = { away: [], returns: 0 };
  const getPinnedOrigin = typeof pinned === "function" ? pinned : () => pinned;
  const watch = registerServerAwayWatch(webContents, {
    getPinnedOrigin,
    onAway: (returnUrl) => events.away.push(returnUrl),
    onReturn: () => events.returns++,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
    ...opts,
  });
  return { webContents, timers, events, watch };
}

describe("registerServerAwayWatch", () => {
  it("notifies after the delay with the last on-server subpage URL", () => {
    const { webContents, timers, events } = makeWatch();

    // On-server navigation (a Databricks /omnigent mount subpage with args)
    // is remembered as the return target.
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent/c/abc123?o=456`);
    // SSO redirect away starts the countdown.
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    assert.equal(timers.pending(), 1);
    assert.deepEqual(events.away, []);

    timers.run();
    assert.deepEqual(events.away, [`${ORIGIN}/omnigent/c/abc123?o=456`]);
  });

  it("does not notify when the window returns to the server before the delay", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/sso`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/sso`);
    assert.equal(timers.pending(), 1);

    // The SSO flow hands back before the timer fires.
    webContents.url = `${ORIGIN}/omnigent`;
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    assert.equal(timers.pending(), 0);
    // onReturn fires on EVERY on-server commit (the initial one included);
    // hiding the banner is idempotent.
    assert.equal(events.returns, 2);

    timers.run();
    assert.deepEqual(events.away, []);
  });

  it("restarts the countdown on each foreign commit (long SSO flows)", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/step1`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/step1`);
    webContents.url = `${FOREIGN}/step2`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/step2`);
    assert.equal(timers.pending(), 1); // re-armed, not stacked

    timers.run();
    assert.equal(events.away.length, 1);
  });

  it("is inert while unpinned (setup page)", () => {
    const { webContents, timers, events } = makeWatch({ pinned: null });
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    assert.equal(timers.pending(), 0);
    timers.run();
    assert.deepEqual(events.away, []);
  });

  it("cancels a pending timer when the window unpins mid-flow", () => {
    let pinned = ORIGIN;
    const { webContents, timers, events } = makeWatch({ pinned: () => pinned });
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    assert.equal(timers.pending(), 1);

    // The HTTP-error fallback unpins the window and loads the setup page:
    // the pending banner timer must die with the pin.
    pinned = null;
    webContents.url = "file:///app/setup/index.html";
    webContents.emit("did-navigate", {}, "file:///app/setup/index.html");
    assert.equal(timers.pending(), 0);
    timers.run();
    assert.deepEqual(events.away, []);
  });

  it("does not re-notify on further foreign navigations until it returns on-server", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    timers.run();
    assert.equal(events.away.length, 1);

    // The user dismissed the banner and keeps browsing the IdP page.
    webContents.url = `${FOREIGN}/mfa`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/mfa`);
    assert.equal(timers.pending(), 0);

    // Returning on-server re-arms the watch for the next away episode.
    webContents.url = `${ORIGIN}/omnigent/c/xyz`;
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent/c/xyz`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    timers.run();
    assert.deepEqual(events.away, [`${ORIGIN}/omnigent`, `${ORIGIN}/omnigent/c/xyz`]);
  });

  it("reset() re-arms after an explicit return attempt lands on SSO again", () => {
    const { webContents, timers, events, watch } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    timers.run();
    assert.equal(events.away.length, 1);

    // "Go back" reloads the server URL, but the session expired and the
    // load lands straight back on the IdP — this must notify again.
    watch.reset();
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    assert.equal(timers.pending(), 1);
    timers.run();
    assert.equal(events.away.length, 2);
  });

  it("tracks in-page navigations on the server as return targets", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.emit("did-navigate-in-page", {}, `${ORIGIN}/omnigent/c/new-chat?o=7`, true);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    timers.run();
    assert.deepEqual(events.away, [`${ORIGIN}/omnigent/c/new-chat?o=7`]);
  });

  it("ignores in-page navigations from subframes", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.emit("did-navigate-in-page", {}, `${FOREIGN}/iframe`, false);
    assert.equal(timers.pending(), 0);
    void events;
  });

  it("re-verifies the current URL at fire time instead of trusting the event", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    // State changed without a commit event (e.g. load failure left the
    // window back on the server): the stale timer must not fire.
    webContents.url = `${ORIGIN}/omnigent`;
    timers.run();
    assert.deepEqual(events.away, []);
  });

  it("dispose() stops all future reports", () => {
    const { webContents, timers, events, watch } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${FOREIGN}/login`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/login`);
    watch.dispose();
    assert.equal(timers.pending(), 0);
    timers.run();
    assert.deepEqual(events.away, []);
  });

  it("exports a sane default delay", () => {
    assert.equal(AWAY_BANNER_DELAY_MS, 10_000);
  });

  it("treats same-origin auth-gate pages as away, never as return targets", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);

    // Session expired: the workspace redirects to its login page ON THE SAME
    // ORIGIN. It must not count as "back", and must not replace the return
    // target (the regression: the banner offered to "return" to login.html).
    webContents.url = `${ORIGIN}/login.html?next_url=%2Fomnigent&o=1`;
    webContents.emit("did-navigate", {}, `${ORIGIN}/login.html?next_url=%2Fomnigent&o=1`);
    assert.equal(timers.pending(), 1);
    assert.equal(events.returns, 1); // only the initial on-server commit

    // Continuing to the actual IdP keeps the episode going; the offer must
    // still name the last real app page.
    webContents.url = `${FOREIGN}/sso`;
    webContents.emit("did-navigate", {}, `${FOREIGN}/sso`);
    timers.run();
    assert.deepEqual(events.away, [`${ORIGIN}/omnigent`]);
  });

  it("treats the front-door OIDC namespace as away on the same origin", () => {
    const { webContents, timers, events } = makeWatch();
    webContents.emit("did-navigate", {}, `${ORIGIN}/omnigent`);
    webContents.url = `${ORIGIN}/oidc/oauth2/v2.0/authorize?client_id=x`;
    webContents.emit("did-navigate", {}, `${ORIGIN}/oidc/oauth2/v2.0/authorize?client_id=x`);
    assert.equal(timers.pending(), 1);
    timers.run();
    assert.deepEqual(events.away, [`${ORIGIN}/omnigent`]);
  });
});

describe("isAuthGatePath", () => {
  it("matches the platform auth namespace, not app pages", () => {
    assert.equal(isAuthGatePath("https://h/login.html?next_url=%2Fomnigent"), true);
    assert.equal(isAuthGatePath("https://h/login"), true);
    assert.equal(isAuthGatePath("https://h/oidc/oauth2/v2.0/authorize"), true);
    assert.equal(isAuthGatePath("https://h/.auth/callback?code=1"), true);
    // App pages and the app's own auth flow are not auth gates.
    assert.equal(isAuthGatePath("https://h/omnigent"), false);
    assert.equal(isAuthGatePath("https://h/auth/login"), false);
    assert.equal(isAuthGatePath("https://h/auth/callback"), false);
    assert.equal(isAuthGatePath("https://h/loginfoo"), false);
    assert.equal(isAuthGatePath("not a url"), false);
  });
});
