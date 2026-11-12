"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const { afterEach, describe, it } = require("node:test");

const { registerTestOverride, withTestOverrides } = require("../src/testOverrides");

const originalRequire = Module.prototype.require;

/** Make `require("electron")` return `electron` in every module. */
function stubRequireElectron(electron) {
  Module.prototype.require = function (id, ...args) {
    if (id === "electron") return electron;
    return originalRequire.call(this, id, ...args);
  };
}

/** Make `require("electron")` return an app with the given packaging state. */
function stubElectron(isPackaged) {
  stubRequireElectron({ app: { isPackaged } });
}

afterEach(() => {
  Module.prototype.require = originalRequire;
  delete globalThis.omnigentTestOverrides;
});

describe("withTestOverrides", () => {
  it("applies overrides in registration order in an unpackaged app", () => {
    stubElectron(false);
    const original = { greet: (name) => `hello ${name}`, keep: () => "kept" };
    registerTestOverride("greeter", () => ({ greet: () => "replaced" }));
    registerTestOverride("greeter", ({ greet }) => ({ greet: (name) => `${greet(name)}!` }));

    const overridden = withTestOverrides("greeter", original);

    assert.equal(overridden.greet("ada"), "replaced!");
    assert.equal(overridden.keep, original.keep);
    assert.equal(original.greet("ada"), "hello ada");
  });

  it("ignores registered overrides in a packaged app", () => {
    stubElectron(true);
    const original = { greet: () => "hello" };
    registerTestOverride("greeter", () => ({ greet: () => "replaced" }));

    assert.equal(withTestOverrides("greeter", original), original);
  });

  it("ignores overrides when Electron reports no packaging state", () => {
    // Plain Node resolves the electron package to its binary path.
    stubRequireElectron("/path/to/electron");
    const original = { greet: () => "hello" };
    registerTestOverride("greeter", () => ({ greet: () => "replaced" }));

    assert.equal(withTestOverrides("greeter", original), original);
  });

  it("returns the exports unchanged without loading Electron when nothing is registered", () => {
    Module.prototype.require = function (id, ...args) {
      assert.notEqual(id, "electron");
      return originalRequire.call(this, id, ...args);
    };
    const original = { greet: () => "hello" };

    assert.equal(withTestOverrides("greeter", original), original);
    registerTestOverride("other", () => ({ greet: () => "replaced" }));
    assert.equal(withTestOverrides("greeter", original), original);
  });

  it("overrides exports of an ES module namespace", async () => {
    stubElectron(false);
    const namespace = await import("data:text/javascript,export const answer = () => 1;");
    registerTestOverride("answers", () => ({ answer: () => 2 }));

    assert.equal(withTestOverrides("answers", namespace).answer(), 2);
    assert.equal(namespace.answer(), 1);
  });
});
