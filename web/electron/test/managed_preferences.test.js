"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  MAX_SERVER_URLS,
  SERVER_URLS_KEY,
  excludingManagedServers,
  getManagedServerUrls,
  parseManagedServerUrls,
} = require("../src/managed_preferences");

describe("managed server preferences", () => {
  it("reads the macOS serverUrls array and preserves workspace paths", () => {
    const calls = [];
    const urls = getManagedServerUrls({
      platform: "darwin",
      getUserDefault: (...args) => {
        calls.push(args);
        return ["omnigent.example.com", "https://workspace.example.com/ml/omnigents?o=123"];
      },
    });

    assert.deepEqual(calls, [[SERVER_URLS_KEY, "array"]]);
    assert.deepEqual(urls, [
      "https://omnigent.example.com/",
      "https://workspace.example.com/ml/omnigents?o=123",
    ]);
  });

  it("does not read macOS preferences on other platforms", () => {
    let reads = 0;
    const urls = getManagedServerUrls({
      platform: "win32",
      getUserDefault: () => {
        reads += 1;
        return ["https://omnigent.example.com"];
      },
    });

    assert.deepEqual(urls, []);
    assert.equal(reads, 0);
  });

  it("deduplicates by origin while keeping the first configured URL", () => {
    assert.deepEqual(
      parseManagedServerUrls([
        "https://workspace.example.com/ml/omnigents",
        "https://workspace.example.com/another-mount",
        "https://other.example.com",
      ]),
      ["https://workspace.example.com/ml/omnigents", "https://other.example.com/"],
    );
  });

  it("rejects an invalid configuration as a whole", () => {
    assert.deepEqual(parseManagedServerUrls("https://omnigent.example.com"), []);
    assert.deepEqual(
      parseManagedServerUrls(["https://valid.example.com", "http://insecure.example.com"]),
      [],
    );
    assert.deepEqual(
      parseManagedServerUrls(Array.from({ length: MAX_SERVER_URLS + 1 }, (_, i) => `host${i}.com`)),
      [],
    );
  });

  it("fails closed when NSUserDefaults cannot be read", () => {
    assert.deepEqual(
      getManagedServerUrls({
        platform: "darwin",
        getUserDefault: () => {
          throw new Error("NSUserDefaults unavailable");
        },
      }),
      [],
    );
  });

  it("filters recents already represented by a managed origin", () => {
    assert.deepEqual(
      excludingManagedServers(
        [
          "https://workspace.example.com/old-mount",
          "https://personal.example.com/",
          "hand-edited-invalid-value",
          null,
        ],
        ["https://workspace.example.com/ml/omnigents"],
      ),
      ["https://personal.example.com/", "hand-edited-invalid-value"],
    );
  });
});
