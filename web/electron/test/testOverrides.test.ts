import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { afterEach, describe, it } from "node:test";

import { registerTestOverride, withTestOverrides } from "../src/testOverrides.ts";

const require = createRequire(import.meta.url);
const originalRequire = Module.prototype.require;

/** Replace `require` in every module; `load` answers an id or defers to Node. */
function stubRequire(load: (id: string) => { value: unknown } | undefined) {
  Module.prototype.require = function (this: Module, id: string, ...args: unknown[]): unknown {
    const stubbed = load(id);
    if (stubbed) return stubbed.value;
    return Reflect.apply(originalRequire, this, [id, ...args]);
  };
}

/** Make `require("electron")` return `electron` in every module. */
function stubRequireElectron(electron: unknown) {
  stubRequire((id) => (id === "electron" ? { value: electron } : undefined));
}

/** Make `require("electron")` return an app with the given packaging state. */
function stubElectron(isPackaged: boolean) {
  stubRequireElectron({ app: { isPackaged } });
}

afterEach(() => {
  Module.prototype.require = originalRequire;
  Reflect.deleteProperty(globalThis, "omnigentTestOverrides");
});

describe("withTestOverrides", () => {
  it("applies overrides in registration order in an unpackaged app", () => {
    stubElectron(false);
    const original = { greet: (name: string) => `hello ${name}`, keep: () => "kept" };
    registerTestOverride("greeter", () => ({ greet: () => "replaced" }));
    registerTestOverride("greeter", ({ greet }) => ({
      greet: (name: string) => `${(greet as (name: string) => string)(name)}!`,
    }));

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
    stubRequire((id) => {
      assert.notEqual(id, "electron");
      return undefined;
    });
    const original = { greet: () => "hello" };

    assert.equal(withTestOverrides("greeter", original), original);
    registerTestOverride("other", () => ({ greet: () => "replaced" }));
    assert.equal(withTestOverrides("greeter", original), original);
  });

  it("overrides exports of an ES module namespace", async () => {
    stubElectron(false);
    const source = "data:text/javascript,export const answer = () => 1;";
    const namespace: { answer: () => number } = await import(source);
    registerTestOverride("answers", () => ({ answer: () => 2 }));

    assert.equal(withTestOverrides("answers", namespace).answer(), 2);
    assert.equal(namespace.answer(), 1);
  });

  it("reaches the onboarding module's default path resolver", async () => {
    stubElectron(false);
    let resolved = 0;
    registerTestOverride("arca", () => ({
      resolveArcaPath: () => {
        resolved += 1;
        return null;
      },
    }));
    // First load in this process, so the module reads its dependency after the override.
    const {
      startArcaSetup,
    }: {
      startArcaSetup: (url: string) => { command: unknown; promise: Promise<unknown> };
    } = require("../src/arca_onboarding.ts");

    const run = startArcaSetup("https://example.com");

    assert.equal(resolved, 1);
    assert.equal(run.command, null);
    assert.deepEqual(await run.promise, {
      ok: false,
      error: "The arca CLI was not found on this machine.",
    });
  });
});
