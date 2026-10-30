"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { DEV_DOMAIN, getDevUserDefault } = require("../src/dev_preferences");

test("unpackaged preferences read the dev domain and preserve value types", () => {
  const calls = [];
  const exec = (binary, args, options) => {
    calls.push([binary, args]);
    if (binary.endsWith("defaults")) return "<plist/>";
    assert.equal(options.input, "<plist/>");
    return JSON.stringify({ serverUrls: ["https://example.com"], DeveloperMode: true });
  };
  assert.deepEqual(getDevUserDefault("serverUrls", "array", { exec }), ["https://example.com"]);
  assert.equal(getDevUserDefault("DeveloperMode", "boolean", { exec }), true);
  assert.deepEqual(calls[0], ["/usr/bin/defaults", ["export", DEV_DOMAIN, "-"]]);
  assert.deepEqual(calls[1], ["/usr/bin/plutil", ["-convert", "json", "-o", "-", "-"]]);
});

test("missing or mistyped dev preferences fail closed", () => {
  const exec = (binary) => (binary.endsWith("defaults") ? "<plist/>" : '{"DeveloperMode":"yes"}');
  assert.equal(getDevUserDefault("DeveloperMode", "boolean", { exec }), false);
  assert.equal(getDevUserDefault("serverUrls", "array", { exec }), null);
  assert.equal(getDevUserDefault("serverNames", "dictionary", { exec }), null);
  assert.equal(
    getDevUserDefault("serverUrls", "array", {
      exec: () => {
        throw Error("missing");
      },
    }),
    null,
  );
});
