"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  DATABRICKS_INTERNAL_FEATURES_KEY,
  MAX_SERVER_URLS,
  SERVER_URLS_KEY,
  excludingManagedServers,
  getDatabricksInternalFeaturesEnabled,
  getManagedServerNames,
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

  it("reads the Databricks-internal-features boolean on macOS", () => {
    const calls = [];
    const enabled = getDatabricksInternalFeaturesEnabled({
      platform: "darwin",
      getUserDefault: (...args) => {
        calls.push(args);
        return true;
      },
    });

    assert.deepEqual(calls, [[DATABRICKS_INTERNAL_FEATURES_KEY, "boolean"]]);
    assert.equal(enabled, true);
  });

  it("fails the internal-features flag closed", () => {
    // Non-darwin platforms never read preferences.
    let reads = 0;
    assert.equal(
      getDatabricksInternalFeaturesEnabled({
        platform: "linux",
        getUserDefault: () => {
          reads += 1;
          return true;
        },
      }),
      false,
    );
    assert.equal(reads, 0);
    // Only an explicit boolean true enables; truthy junk does not.
    for (const value of [undefined, null, "true", 1, [true]]) {
      assert.equal(
        getDatabricksInternalFeaturesEnabled({
          platform: "darwin",
          getUserDefault: () => value,
        }),
        false,
      );
    }
    // A read error also reads as disabled.
    assert.equal(
      getDatabricksInternalFeaturesEnabled({
        platform: "darwin",
        getUserDefault: () => {
          throw new Error("NSUserDefaults unavailable");
        },
      }),
      false,
    );
  });

  it("reads display names from an omnigentServerName query parameter", () => {
    const calls = [];
    const getUserDefault = (...args) => {
      calls.push(args);
      return [
        "https://workspace.example.com/?o=123&omnigentServerName=%20Team%20A%20",
        "https://other.example.com/ml?omnigentServerName=",
        "plain.example.com",
      ];
    };
    // The parameter never reaches the server URL.
    assert.deepEqual(getManagedServerUrls({ platform: "darwin", getUserDefault }), [
      "https://workspace.example.com/?o=123",
      "https://other.example.com/ml",
      "https://plain.example.com/",
    ]);
    assert.deepEqual(getManagedServerNames({ platform: "darwin", getUserDefault }), {
      "https://workspace.example.com/?o=123": "Team A",
    });
    assert.deepEqual(calls, [
      [SERVER_URLS_KEY, "array"],
      [SERVER_URLS_KEY, "array"],
    ]);
  });

  it("names only the servers the list keeps", () => {
    const getUserDefault = () => [
      "https://team.example.com/",
      "https://team.example.com/other?omnigentServerName=Dropped",
    ];
    assert.deepEqual(getManagedServerNames({ platform: "darwin", getUserDefault }), {});
    assert.deepEqual(
      getManagedServerNames({
        platform: "darwin",
        getUserDefault: () => ["https://team.example.com/?omnigentServerName=Team", "http://bad"],
      }),
      {},
    );
    assert.deepEqual(getManagedServerNames({ platform: "linux", getUserDefault }), {});
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
