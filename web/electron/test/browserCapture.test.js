// Tests for src/browserCapture.js, run with `node --test`. Fake view, window
// and image objects; no Electron.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { captureBrowserView } = require("../src/browserCapture");

const png = (empty) => ({
  isEmpty: () => empty,
  getSize: () => (empty ? { width: 0, height: 0 } : { width: 10, height: 10 }),
  toPNG: () => Buffer.from("PNG"),
});
const DATA_URL = `data:image/png;base64,${Buffer.from("PNG").toString("base64")}`;

/** captures: array of "empty" | "full" | "hang" | Error, consumed in order (last repeats). */
function setup({ attached = false, captures = ["full"], active = null, visible = true } = {}) {
  const log = [];
  let bounds = { x: 5, y: 6, width: 300, height: 200 };
  let vis = visible;
  let n = 0;
  const view = {
    getBounds: () => ({ ...bounds }),
    setBounds: (b) => {
      bounds = { ...b };
      log.push(["setBounds", { ...b }]);
    },
    getVisible: () => vis,
    setVisible: (v) => {
      vis = v;
      log.push(["setVisible", v]);
    },
    webContents: {
      capturePage: () => {
        log.push(["capture", { ...bounds }]);
        const step = captures[Math.min(n++, captures.length - 1)];
        if (step === "hang") return new Promise(() => {});
        if (step instanceof Error) return Promise.reject(step);
        return Promise.resolve(png(step === "empty"));
      },
    },
  };
  const children = attached ? [view] : [];
  const win = {
    contentView: {
      children,
      addChildView: (v, i) => {
        children.splice(i ?? children.length, 0, v);
        log.push(["add", i]);
      },
      removeChildView: (v) => {
        children.splice(children.indexOf(v), 1);
        log.push(["remove"]);
      },
    },
    getContentBounds: () => ({ x: 0, y: 0, width: 1000, height: 700 }),
  };
  const entry = {
    view,
    boundsController: { resync: () => log.push(["resync"]) },
  };
  let live = entry;
  const registry = {
    activeConversationId: () => active,
    get: (id) => (id === "c1" ? live : null),
  };
  const state = () => ({ bounds, vis, children });
  const close = () => {
    live = null;
  };
  return { log, view, win, entry, registry, state, close };
}

const run = (s, extra = {}) =>
  captureBrowserView({
    registry: s.registry,
    conversationId: "c1",
    entry: s.entry,
    win: s.win,
    wait: async () => {},
    ...extra,
  });

describe("captureBrowserView", () => {
  it("captures an attached view without touching children", async () => {
    const s = setup({ attached: true, active: "c1" });
    const r = await run(s);
    assert.deepEqual(r, { ok: true, dataUrl: DATA_URL });
    assert.deepEqual(
      s.log.map((l) => l[0]),
      ["capture"],
    );
  });

  it("stages a detached view at index 0 offscreen and restores it", async () => {
    const s = setup({ active: "other", visible: false });
    const r = await run(s);
    assert.equal(r.ok, true);
    const add = s.log.findIndex((l) => l[0] === "add");
    assert.equal(s.log[add][1], 0);
    const cap = s.log.find((l) => l[0] === "capture");
    assert.deepEqual(cap[1], { x: 999, y: 699, width: 300, height: 200 });
    assert.equal(s.state().children.length, 0);
    assert.deepEqual(s.state().bounds, { x: 5, y: 6, width: 300, height: 200 });
    assert.equal(s.state().vis, false);
  });

  it("uses the window size when saved bounds are empty", async () => {
    const s = setup({ active: "other" });
    s.view.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    await run(s);
    const cap = s.log.find((l) => l[0] === "capture");
    assert.deepEqual(cap[1], { x: 999, y: 699, width: 1000, height: 700 });
  });

  it("retries a never-painted view until it paints", async () => {
    const s = setup({ active: "other", captures: ["empty", "empty", "full"] });
    const r = await run(s);
    assert.equal(r.ok, true);
    assert.equal(s.log.filter((l) => l[0] === "capture").length, 3);
  });

  it("errors and cleans up when still empty after maxWaitMs", async () => {
    const s = setup({ active: "other", captures: ["empty"] });
    const r = await run(s, { retryMs: 50, maxWaitMs: 100 });
    assert.equal(r.ok, false);
    assert.match(r.error, /not painted yet/);
    assert.equal(s.state().children.length, 0);
    assert.deepEqual(s.state().bounds, { x: 5, y: 6, width: 300, height: 200 });
  });

  it("leaves the view attached and resyncs when the chat became active", async () => {
    const s = setup({ active: "c1" });
    const r = await run(s);
    assert.equal(r.ok, true);
    assert.equal(s.state().children.length, 1);
    assert.ok(s.log.some((l) => l[0] === "resync"));
    assert.ok(!s.log.some((l) => l[0] === "remove"));
    // The staging position is undone before resync, which applies nothing
    // until the pane has sent bounds.
    assert.deepEqual(s.state().bounds, { x: 5, y: 6, width: 300, height: 200 });
  });

  it("counts slow empty captures against the retry budget", async () => {
    const s = setup({ active: "other", captures: ["empty"] });
    const slow = s.view.webContents.capturePage;
    s.view.webContents.capturePage = () =>
      new Promise((resolve) => {
        setTimeout(() => resolve(slow()), 30);
      });
    const r = await run(s, { maxWaitMs: 50 });
    assert.equal(r.ok, false);
    assert.ok(s.log.filter((l) => l[0] === "capture").length <= 3);
    assert.equal(s.state().children.length, 0);
  });

  it("refuses a view that was closed before its queued capture started", async () => {
    const s = setup({ active: "other" });
    s.close();
    const r = await run(s);
    assert.equal(r.ok, false);
    assert.match(r.error, /was closed/);
    assert.ok(!s.log.some((l) => l[0] === "add" || l[0] === "capture"));
  });

  it("removes a staged view whose chat was closed mid-capture, even if reopened", async () => {
    const s = setup({ active: "c1", captures: ["empty", "full"] });
    // Closed and reopened as a new entry for the same chat while staged.
    const r = await run(s, {
      wait: async () => {
        s.close();
      },
    });
    assert.equal(r.ok, true);
    assert.equal(s.state().children.length, 0);
    assert.ok(!s.log.some((l) => l[0] === "resync"));
  });

  it("times out on a hang and cleans up", async () => {
    const s = setup({ active: "other", captures: ["hang"] });
    const r = await run(s, { timeoutMs: 20 });
    assert.equal(r.ok, false);
    assert.match(r.error, /screenshot timed out after 0 s/);
    assert.equal(s.state().children.length, 0);
    assert.deepEqual(s.state().bounds, { x: 5, y: 6, width: 300, height: 200 });
  });

  it("reports a 10 s timeout message by default seconds", async () => {
    const s = setup({ attached: true, active: "c1", captures: ["hang"] });
    const r = await run(s, { timeoutMs: 1000 });
    assert.match(r.error, /timed out after 1 s: the browser view did not paint/);
  });

  it("returns the rejection message and cleans up", async () => {
    const s = setup({ active: "other", captures: [new Error("UnknownVizError")] });
    const r = await run(s);
    assert.deepEqual(r, { ok: false, error: "UnknownVizError" });
    assert.equal(s.state().children.length, 0);
  });

  it("serializes concurrent captures of one view", async () => {
    const s = setup({ active: "other" });
    const [a, b] = await Promise.all([run(s), run(s)]);
    assert.equal(a.ok && b.ok, true);
    const seq = s.log.map((l) => l[0]).filter((k) => k === "add" || k === "remove");
    assert.deepEqual(seq, ["add", "remove", "add", "remove"]);
  });
});
