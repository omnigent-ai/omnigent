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

function harness({ url = PAGE, canPrompt = () => true } = {}) {
  const contents = new EventEmitter();
  let currentUrl = url;
  Object.assign(contents, {
    ipc: new EventEmitter(),
    mainFrame: { url },
    getURL: () => currentUrl,
  });
  const prompts = [];
  const opened = [];
  attachSecurityKeyNotice(contents, {
    canPrompt: (wc) => {
      assert.equal(wc, contents);
      return canPrompt();
    },
    showPrompt: (options) =>
      new Promise((resolve) => {
        const prompt = { options, choose: resolve };
        options.signal.addEventListener("abort", () => resolve("dismiss"));
        prompts.push(prompt);
      }),
    openExternal: (target) => opened.push(target),
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
  afterEach(() => mock.timers.reset());

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

  it("hides the notice when the view closes", async () => {
    const h = harness();
    h.report(true);
    await h.wait();
    h.contents.emit("destroyed");
    assert.equal(h.prompts[0].options.signal.aborted, true);
  });

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
    assert.equal(h.prompts.length, 2);
    assert.equal(h.prompts[1].options.kind, "security-key");
    visible = false;
    h.prompts[1].choose("suppressed");
    await flush();
    assert.equal(h.prompts.length, 2);
    assert.deepEqual(h.opened, []);
  });

  for (const [name, options, frame] of [
    ["a background or covered pane", { canPrompt: () => false }],
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
