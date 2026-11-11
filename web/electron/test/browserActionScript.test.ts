import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { it } from "node:test";

import { buildBrowserActionScript } from "../src/browserActionScript.ts";
import { decodeLegacyBrowserAction } from "../src/browserLegacyAction.ts";

interface LegacyFixture {
  requests: { action: string; args: Record<string, unknown>; script: string }[];
}

const legacy: LegacyFixture = JSON.parse(
  fs.readFileSync(path.join(import.meta.dirname, "fixtures", "browserActions-v0.18.json"), "utf8"),
);

it("translates the frozen v0.18 relay templates into validated actions", () => {
  for (const { action, args, script } of legacy.requests) {
    assert.deepEqual(decodeLegacyBrowserAction(script), { action, params: args });
    assert.equal(buildBrowserActionScript(action, args), script);
  }
});

it("rejects altered legacy templates and accepts only bounded literal data", () => {
  const script = buildBrowserActionScript("type", { ref: 1, text: "Ada" });
  for (const input of [
    null,
    "1 + 1",
    `${script}\n`,
    `${script}; 1 + 1`,
    script.replace("el.focus();", ""),
    script.replace('else el.value = "Ada"', 'else el.value = "Grace"'),
    "x".repeat(2_000_001),
  ])
    assert.throws(() => decodeLegacyBrowserAction(input));
  const args = { ref: 1, snapshot_id: '"'.repeat(128), text: "\n".repeat(100000) };
  assert.deepEqual(decodeLegacyBrowserAction(buildBrowserActionScript("type", args)), {
    action: "type",
    params: args,
  });
  assert.throws(() =>
    decodeLegacyBrowserAction(script.replaceAll('"Ada"', JSON.stringify("x".repeat(100001)))),
  );
});

it("rejects unknown actions, non-data arguments and out-of-range references", () => {
  for (const [action, args] of [
    ["evaluate", {}],
    ["snapshot", { js: "1 + 1" }],
    ["click", null],
    ["click", { ref: -1 }],
    ["click", { ref: Infinity }],
    ["click", { ref: "1" }],
    ["click", {}],
    ["type", { selector: "input", text: {} }],
    ["click", { selector: "x".repeat(4097) }],
  ])
    assert.throws(() => buildBrowserActionScript(action, args));
});

it("accepts text and snapshot identifiers at their size limits", () => {
  assert.doesNotThrow(() => buildBrowserActionScript("type", { ref: 1, text: "x".repeat(100000) }));
  assert.throws(
    () => buildBrowserActionScript("type", { ref: 1, text: "x".repeat(100001) }),
    /text must/,
  );
  assert.doesNotThrow(() =>
    buildBrowserActionScript("click", { ref: 1, snapshot_id: "x".repeat(128) }),
  );
  assert.throws(
    () => buildBrowserActionScript("click", { ref: 1, snapshot_id: "x".repeat(129) }),
    /snapshot_id must/,
  );
});
