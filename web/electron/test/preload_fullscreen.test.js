const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { loadPreload } = require("../test-support/preload_harness");

const CHANNEL = "omnigent:full-screen-changed";

describe("desktop fullscreen bridge", () => {
  it("reads the window's fullscreen state over IPC", async () => {
    const h = loadPreload((channel) =>
      channel === "omnigent:window-is-full-screen" ? true : null,
    );
    assert.equal(await h.desktop.isFullScreen(), true);
    assert.ok(h.invokes.some((call) => call.channel === "omnigent:window-is-full-screen"));
  });

  it("forwards transitions as booleans and unsubscribes cleanly", () => {
    const h = loadPreload();
    const seen = [];
    const unsubscribe = h.desktop.onFullScreenChanged((value) => seen.push(value));

    assert.ok(h.hasListener(CHANNEL), `onFullScreenChanged must subscribe to ${CHANNEL}`);
    h.emit(CHANNEL, true);
    h.emit(CHANNEL, false);
    h.emit(CHANNEL, "junk"); // Non-boolean payloads coerce to false, never leak through.
    assert.deepEqual(seen, [true, false, false]);

    unsubscribe();
    assert.equal(h.hasListener(CHANNEL), false);
  });
});
