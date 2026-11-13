"use strict";

const { describe, it, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  attachSecurityKeyNotice,
  PENDING_CHANNEL,
  SHOW_DELAY_MS,
} = require("../src/browserSecurityKeyNotice");

const PAGE = "https://login.example.com/signin/verify";
const flush = () => new Promise(setImmediate);

function harness({ url = PAGE, canPrompt = () => true, launch, beforeShow } = {}) {
  const contents = new EventEmitter();
  let currentUrl = url;
  Object.assign(contents, {
    ipc: new EventEmitter(),
    mainFrame: { url },
    getURL: () => currentUrl,
    isDestroyed: () => false,
  });
  const prompts = [];
  const opened = [];
  attachSecurityKeyNotice(contents, {
    canPrompt: (wc) => {
      assert.equal(wc, contents);
      return canPrompt();
    },
    showPrompt: (options) => {
      beforeShow?.();
      return new Promise((resolve) => {
        const prompt = { options, choose: resolve };
        options.signal.addEventListener("abort", () => resolve("dismiss"));
        prompts.push(prompt);
      });
    },
    openExternal: (target) => {
      opened.push(target);
      return launch?.();
    },
    cookieImport: true,
  });
  const report = (pending, frame = contents.mainFrame) =>
    contents.ipc.emit(PENDING_CHANNEL, { senderFrame: frame }, pending);
  const wait = async () => {
    mock.timers.tick(SHOW_DELAY_MS);
    await flush();
  };
  return {
    contents,
    prompts,
    opened,
    report,
    wait,
    navigate: (next) => {
      currentUrl = next;
    },
  };
}

describe("security-key notice for embedded browser pages", () => {
  beforeEach(() => mock.timers.enable({ apis: ["setTimeout"] }));
  afterEach(() => {
    mock.restoreAll();
    mock.timers.reset();
  });

  it("shows while the page waits on a security key and hides once the request settles", async () => {
    const h = harness();
    h.report(true);
    h.report(true);
    await h.wait();
    assert.equal(h.prompts.length, 1);
    const { options } = h.prompts[0];
    assert.equal(options.kind, "security-key");
    assert.equal(options.origin, "https://login.example.com");
    assert.equal(options.cookieImport, true);
    h.report(false);
    assert.equal(options.signal.aborted, true);
    assert.deepEqual(h.opened, []);
  });

  it("opens no window for a request that settles before the delay", async () => {
    const h = harness();
    h.report(true);
    h.report(false);
    await h.wait();
    assert.equal(h.prompts.length, 0);
  });

  it("opens the page in the system browser on request and shows again for a new request", async () => {
    const h = harness();
    h.report(true);
    await h.wait();
    h.prompts[0].choose("open-in-browser");
    await flush();
    assert.deepEqual(h.opened, [PAGE]);
    h.report(true);
    await h.wait();
    h.prompts[1].choose("dismiss");
    await flush();
    assert.deepEqual(h.opened, [PAGE]);
    assert.equal(h.prompts.length, 2);
  });

  for (const [name, launch] of [
    ["rejects", () => Promise.reject(new Error("no default browser"))],
    [
      "throws",
      () => {
        throw new Error("no default browser");
      },
    ],
  ]) {
    it(`offers the notice again when opening the system browser ${name}`, async () => {
      const warning = mock.method(console, "warn", () => {});
      const h = harness({ launch });
      h.report(true);
      await h.wait();
      h.prompts[0].choose("open-in-browser");
      await flush();
      assert.deepEqual(h.opened, [PAGE]);
      assert.equal(h.prompts.length, 2);
      assert.equal(warning.mock.callCount(), 1);
    });
  }

  it("keeps one hidden-pane poll when a failed handoff overlaps a pending report", async () => {
    mock.method(console, "warn", () => {});
    let rejectLaunch;
    let visible = true;
    let checks = 0;
    const h = harness({
      canPrompt: () => {
        checks += 1;
        return visible;
      },
      launch: () =>
        new Promise((_resolve, reject) => {
          rejectLaunch = reject;
        }),
    });
    h.report(true);
    await h.wait();
    h.prompts[0].choose("open-in-browser");
    await flush();
    visible = false;
    h.report(true);
    rejectLaunch(new Error("launch failed"));
    await flush();
    checks = 0;
    await h.wait();
    assert.equal(checks, 1, "the handoff failure replaces the already armed poll");
    h.report(false);
    await h.wait();
    assert.equal(checks, 1, "request completion cancels the remaining poll");
  });

  it("ignores an old launch failure after a newer request's notice was dismissed", async () => {
    mock.method(console, "warn", () => {});
    let rejectLaunch;
    const h = harness({
      launch: () =>
        new Promise((_resolve, reject) => {
          rejectLaunch = reject;
        }),
    });
    h.report(true);
    await h.wait();
    h.prompts[0].choose("open-in-browser");
    await flush();
    h.report(false);
    h.report(true);
    await h.wait();
    h.prompts[1].choose("dismiss");
    await flush();
    rejectLaunch(new Error("old launch failed"));
    await flush();
    await h.wait();
    assert.equal(h.prompts.length, 2, "a stale failure cannot undo the new dismissal");
  });

  it("retries after showPrompt throws and recovers on the next attempt", async () => {
    const warning = mock.method(console, "warn", () => {});
    let fail = true;
    const h = harness({
      beforeShow: () => {
        if (fail) throw new Error("popup construction failed");
      },
    });
    h.report(true);
    await h.wait();
    assert.equal(warning.mock.callCount(), 1);
    assert.equal(h.prompts.length, 0);
    fail = false;
    await h.wait();
    assert.equal(h.prompts.length, 1);
    assert.equal(h.prompts[0].options.signal.aborted, false);
  });

  it("does not reopen after a browser launch fails when the view has closed", async () => {
    const warning = mock.method(console, "warn", () => {});
    let rejectLaunch;
    const closing = harness({
      launch: () =>
        new Promise((_resolve, reject) => {
          rejectLaunch = reject;
        }),
    });
    closing.report(true);
    await closing.wait();
    closing.prompts[0].choose("open-in-browser");
    await flush();
    closing.contents.emit("destroyed");
    const getURL = mock.method(closing.contents, "getURL", () => {
      throw new Error("view destroyed");
    });
    rejectLaunch(new Error("late launch failure"));
    await flush();
    assert.equal(closing.prompts.length, 1);
    assert.equal(warning.mock.callCount(), 1);
    assert.equal(getURL.mock.callCount(), 0);
  });

  it("does not hand a different site to the system browser", async () => {
    const h = harness();
    h.report(true);
    await h.wait();
    h.navigate("https://elsewhere.example/");
    h.prompts[0].choose("open-in-browser");
    await flush();
    assert.deepEqual(h.opened, []);
  });

  for (const [name, end] of [
    ["a committed navigation", (h) => h.contents.emit("did-navigate", {}, PAGE)],
    ["a renderer crash", (h) => h.contents.emit("render-process-gone")],
  ]) {
    it(`hides and forgets the request on ${name}`, async () => {
      const h = harness();
      h.report(true);
      await h.wait();
      end(h);
      assert.equal(h.prompts[0].options.signal.aborted, true);
      h.report(true);
      end(h);
      await h.wait();
      assert.equal(h.prompts.length, 1);
    });
  }

  for (const beforeShow of [false, true]) {
    it(`handles view destruction ${beforeShow ? "before" : "after"} showing the notice`, async () => {
      const h = harness();
      h.report(true);
      if (!beforeShow) await h.wait();
      mock.method(h.contents, "isDestroyed", () => true);
      const getURL = mock.method(h.contents, "getURL", () => {
        throw new Error("view destroyed");
      });
      if (beforeShow) {
        await h.wait();
        assert.equal(h.prompts.length, 0);
      } else {
        h.contents.emit("destroyed");
        assert.equal(h.prompts[0].options.signal.aborted, true);
      }
      assert.equal(getURL.mock.callCount(), 0);
    });
  }

  it("keeps the notice while a navigation that may not commit is in flight", async () => {
    const h = harness();
    h.report(true);
    await h.wait();
    h.contents.emit("did-start-navigation", {}, "https://elsewhere.example/", false, true);
    assert.equal(h.prompts[0].options.signal.aborted, false);
  });

  it("shows again after yielding to a permission prompt while the pane can still prompt", async () => {
    let visible = true;
    const h = harness({ canPrompt: () => visible });
    h.report(true);
    await h.wait();
    h.prompts[0].choose("suppressed");
    await flush();
    assert.equal(h.prompts.length, 1, "suppression does not reopen in a microtask loop");
    await h.wait();
    assert.equal(h.prompts.length, 2);
    assert.equal(h.prompts[1].options.kind, "security-key");
    visible = false;
    h.prompts[1].choose("suppressed");
    await flush();
    assert.equal(h.prompts.length, 2);
    visible = true;
    await h.wait();
    assert.equal(h.prompts.length, 3);
    assert.deepEqual(h.opened, []);
  });

  it("shows once a hidden pane can prompt while the request still waits", async () => {
    let visible = false;
    const h = harness({ canPrompt: () => visible });
    h.report(true);
    await h.wait();
    await h.wait();
    assert.equal(h.prompts.length, 0);
    visible = true;
    await h.wait();
    assert.equal(h.prompts.length, 1);
  });

  it("stops waiting for a hidden pane once the request settles", async () => {
    let visible = false;
    const h = harness({ canPrompt: () => visible });
    h.report(true);
    await h.wait();
    h.report(false);
    visible = true;
    await h.wait();
    assert.equal(h.prompts.length, 0);
  });

  for (const [name, options, frame] of [
    ["a non-web page", { url: "file:///tmp/verify.html" }],
    ["a subframe report", {}, { url: "https://frame.example/" }],
  ]) {
    it(`does not prompt for ${name}`, async () => {
      const h = harness(options);
      h.report(true, frame);
      await h.wait();
      assert.equal(h.prompts.length, 0);
    });
  }
});
