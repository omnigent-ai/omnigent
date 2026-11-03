"use strict";

const { buildBrowserActionScript } = require("./browserActionScript.mjs");

// v0.18's relay embeds JSON literals in fixed templates. Remove this adapter in v0.21.
const JSON_STRING = String.raw`"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[\da-fA-F]{4}))*"`;
const placeholders = {
  '"__legacy_selector__"': "selector",
  '"__legacy_snapshot_id__"': "snapshot_id",
  '"__legacy_text__"': "text",
  9007199254740991: "ref",
};

function legacyPattern(action, args) {
  const seen = new Set();
  const parts = buildBrowserActionScript(action, args).split(
    /("__legacy_selector__"|"__legacy_snapshot_id__"|"__legacy_text__"|9007199254740991)/,
  );
  const pattern = parts.map((part) => {
    const key = placeholders[part];
    if (!key) return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (seen.has(key)) return `\\k<${key}>`;
    seen.add(key);
    return `(?<${key}>${key === "ref" ? "[0-9]+" : JSON_STRING})`;
  });
  return { action, pattern: new RegExp(`^${pattern.join("")}$`) };
}

const patterns = ["click", "type"].flatMap((action) =>
  [
    { selector: "__legacy_selector__" },
    { ref: Number.MAX_SAFE_INTEGER },
    { ref: Number.MAX_SAFE_INTEGER, snapshot_id: "__legacy_snapshot_id__" },
  ].map((args) =>
    legacyPattern(action, action === "type" ? { ...args, text: "__legacy_text__" } : args),
  ),
);

function decodeLegacyBrowserAction(script) {
  if (typeof script === "string" && script.length <= 2_000_000) {
    if (script === buildBrowserActionScript("snapshot")) {
      return { action: "snapshot", params: {} };
    }
    for (const { action, pattern } of patterns) {
      const match = pattern.exec(script);
      if (!match) continue;
      const params = Object.fromEntries(
        Object.entries(match.groups).map(([key, value]) => [key, JSON.parse(value)]),
      );
      // Validate data limits and require the complete canonical template; never evaluate input.
      if (buildBrowserActionScript(action, params) === script) return { action, params };
    }
  }
  throw new Error("Unsupported legacy browser action; update the server UI");
}

module.exports = { decodeLegacyBrowserAction };
