"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { DEVELOPER_MODE_KEY, isDeveloperModeEnabled } = require("../src/developer_mode");

describe("developer mode", () => {
  it("is always enabled for development builds", () => {
    assert.equal(
      isDeveloperModeEnabled({
        isPackaged: false,
        platform: "linux",
        getUserDefault: () => false,
      }),
      true,
    );
  });

  it("reads the packaged macOS opt-in as a boolean user default", () => {
    const calls = [];
    const enabled = isDeveloperModeEnabled({
      isPackaged: true,
      platform: "darwin",
      getUserDefault: (...args) => {
        calls.push(args);
        return true;
      },
    });

    assert.equal(enabled, true);
    assert.deepEqual(calls, [[DEVELOPER_MODE_KEY, "boolean"]]);
  });

  it("stays disabled when the packaged macOS preference is false", () => {
    assert.equal(
      isDeveloperModeEnabled({
        isPackaged: true,
        platform: "darwin",
        getUserDefault: () => false,
      }),
      false,
    );
  });

  it("stays disabled for packaged builds on other platforms", () => {
    let reads = 0;
    const enabled = isDeveloperModeEnabled({
      isPackaged: true,
      platform: "win32",
      getUserDefault: () => {
        reads += 1;
        return true;
      },
    });

    assert.equal(enabled, false);
    assert.equal(reads, 0);
  });

  it("fails closed if macOS user defaults cannot be read", () => {
    assert.equal(
      isDeveloperModeEnabled({
        isPackaged: true,
        platform: "darwin",
        getUserDefault: () => {
          throw new Error("NSUserDefaults unavailable");
        },
      }),
      false,
    );
  });
});
