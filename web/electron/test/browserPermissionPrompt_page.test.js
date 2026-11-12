"use strict";

// The permission prompt page script, run against a minimal fake DOM.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");

const ORIGIN = "https://login.example.com";
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

function pageHarness(info = { origin: ORIGIN, reload: true }) {
  const elements = new Map();
  for (const id of ["origin", "reload", "once", "always", "deny", "close"]) {
    elements.set(id, {
      disabled: id !== "close",
      listeners: {},
      addEventListener(name, fn) {
        this.listeners[name] = fn;
      },
      focus() {
        this.focused = true;
      },
    });
  }
  const events = {};
  const choices = [];
  const document = {
    getElementById: (id) => elements.get(id),
    querySelectorAll: (selector) =>
      (selector === "footer button"
        ? ["once", "always", "deny"]
        : ["once", "always", "deny", "close"]
      ).map((id) => elements.get(id)),
    addEventListener: (name, fn) => {
      events[name] = fn;
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../browser-permission/index.js"), "utf8"),
    {
      document,
      window: {
        omnigentBrowserPermission: {
          getInfo: async () => info,
          choose: async (choice) => {
            choices.push(choice);
          },
        },
      },
    },
  );
  return { elements, events, choices };
}

describe("permission popover page", () => {
  it("packages the compact UI with exactly the requested three choices", () => {
    const html = fs.readFileSync(path.join(__dirname, "../browser-permission/index.html"), "utf8");
    assert.ok(require("../package.json").build.files.includes("browser-permission/**/*"));
    for (const label of ["Allow once", "Always allow", "Deny"]) assert.ok(html.includes(label));
    assert.match(html, /default-src 'none'/);
    assert.doesNotMatch(html, /Okta Verify|Only allow sites you trust|Always allow and reload/);
  });

  for (const [id, choice] of [
    ["once", "allow-once"],
    ["always", "always-allow"],
    ["deny", "deny"],
    ["close", "dismiss"],
  ]) {
    it(`maps ${id} to ${choice} and ignores repeated submissions`, async () => {
      const h = pageHarness();
      await tick();
      h.elements.get(id).listeners.click();
      h.elements.get("always").listeners.click();
      assert.deepEqual(h.choices, [choice]);
    });
  }

  it("renders the origin as text, shows the reload hint, and focuses dismissal instead of approval", async () => {
    const origin = "https://long.example/<not-markup>";
    const h = pageHarness({ origin, reload: true });
    await tick();
    assert.equal(h.elements.get("origin").textContent, origin);
    assert.equal(h.elements.get("reload").hidden, false);
    assert.equal(h.elements.get("close").focused, true);
    h.events.keydown({ key: "Escape" });
    assert.deepEqual(h.choices, ["dismiss"]);
    const withoutReload = pageHarness({ origin: ORIGIN, reload: false });
    await tick();
    assert.equal(withoutReload.elements.get("reload").hidden, true);
  });
});
