"use strict";

// Main says when a newer navigation of a view took over from
// a cancelled one, so the agent's new tab isn't dropped from under the user.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { registerBrowserIpc } = require("../src/browserIpc");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");

function setup() {
  const handlers = new Map();
  const ipcMain = {
    handle: (channel, fn) => handlers.set(channel, fn),
  };
  const registry = createBrowserViewRegistry();
  const pending = [];
  registry.checkAgentNavigation = (_id, url) =>
    url.includes("slow")
      ? new Promise((resolve) => {
          pending.push(resolve);
        })
      : { ok: true };
  registry.openOrNavigate = () => ({ ok: true, created: false });
  registerBrowserIpc({
    ipcMain,
    isPinnedOriginSender: () => true,
    getRegistryForEvent: () => registry,
  });
  const navigate = (url, opts) =>
    handlers.get("omnigent:browser-open-or-navigate")({}, { conversationId: "v", url, opts });
  return { registry, pending, navigate };
}

describe("a cancelled navigation", () => {
  it("is superseded when a newer one took the view, not when it timed out", async () => {
    const taken = setup();
    const agent = taken.navigate("https://slow.example/", { agent: true });
    // The user types an address in the tab while the agent's check runs.
    assert.equal((await taken.navigate("https://user.example/")).ok, true);
    taken.pending[0]({ ok: true });
    const result = await agent;
    assert.equal(result.ok, false);
    // Across IPC, like the renderer gets it.
    assert.equal(structuredClone(result).superseded, true);

    const expired = setup();
    const late = expired.navigate("https://slow.example/", {
      agent: true,
      deadlineAt: Date.now() + 10,
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expired.pending[0]({ ok: true });
    const timedOut = await late;
    assert.equal(timedOut.ok, false);
    assert.equal(structuredClone(timedOut).superseded, false);
  });
});
