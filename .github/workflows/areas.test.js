// Integrity checks for .github/areas.json -- the single source of truth for both
// issue triage and PR reviewer assignment. Run offline: `node .github/workflows/areas.test.js`
// (cwd = repo root). No network. Guards the invariants the two workflows rely on.
const fs = require("fs");
const path = require("path");

const areas = JSON.parse(fs.readFileSync(path.resolve(".github/areas.json"), "utf8")).areas;
const priorityLabels = new Set(
  JSON.parse(fs.readFileSync(path.resolve(".github/issue-prioritization-labels.json"), "utf8"))
    .labels.map((label) => label.name),
);
const maint = new Set(
  fs.readFileSync(path.resolve(".github/MAINTAINER"), "utf8")
    .split("\n").map((l) => l.replace(/#.*/, "").trim().toLowerCase()).filter(Boolean)
);

// The 8 comp:* labels that exist in the repo (gh cannot add a label that does not
// exist, and there is no label-sync). Every area label must be one of these.
const ALLOWED_LABELS = new Set([
  "comp:server", "comp:runner", "comp:repr", "comp:web-ui",
  "comp:tui", "comp:policies", "comp:harnesses", "comp:infra",
]);

let failures = 0;
function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) failures++;
}

// Every owner is a known maintainer.
for (const a of areas)
  for (const o of a.owners || [])
    assert(`owner @${o} (area ${a.key}) is in MAINTAINER`, maint.has(o.toLowerCase()));

// Every label is one of the real comp:* labels.
for (const a of areas)
  assert(`area ${a.key} label ${a.label} is a real comp:*`, ALLOWED_LABELS.has(a.label));

// V2 labels are declared separately so the active triage workflow can keep
// using the legacy label until issue prioritization is enabled.
for (const a of areas)
  assert(
    `area ${a.key} priority_label ${a.priority_label} is declared`,
    priorityLabels.has(a.priority_label),
  );

// Every area has >= 2 owners (the 2+ codeowner requirement). Paused owners
// still count -- pausing someone must not force adding a new active owner.
for (const a of areas) {
  const n = (a.owners || []).length + (a.owners_paused || []).length;
  assert(`area ${a.key} has >= 2 owners`, n >= 2, `${n} owner(s)`);
}

// Every area has a definition and at least one path.
for (const a of areas) {
  assert(`area ${a.key} has a definition`, typeof a.definition === "string" && a.definition.length > 0);
  assert(`area ${a.key} has paths`, Array.isArray(a.paths) && a.paths.length > 0);
}

// Every area has a weight (importance multiplier for the priority score) drawn
// from the allowed bands, tagged with its source (telemetry vs editorial).
const ALLOWED_WEIGHTS = new Set([1.4, 1.2, 1.1, 1.0, 0.9]);
const ALLOWED_WEIGHT_SOURCES = new Set(["telemetry", "editorial"]);
for (const a of areas) {
  assert(`area ${a.key} weight is an allowed band`, ALLOWED_WEIGHTS.has(a.weight), `${a.weight}`);
  assert(
    `area ${a.key} weight_source is telemetry|editorial`,
    ALLOWED_WEIGHT_SOURCES.has(a.weight_source),
    `${a.weight_source}`,
  );
}

// Path resolution (last-match-wins startsWith) sends representative files to the
// expected area -- especially the web/ carve-out ordering and harness prefixes.
function resolve(fn) {
  let match = null;
  for (const a of areas) for (const p of a.paths) if (fn.startsWith(p)) match = a;
  return match;
}
const cases = [
  ["omnigent/inner/foo.py", "inner"],
  ["omnigent/inner/claude_sdk_executor.py", "harness-claude"],
  ["omnigent/inner/kimi_executor.py", "harness-kimi"],
  ["omnigent/inner/kiro_native_harness.py", "harness-kiro"],
  ["web/src/main.tsx", "web"],
  ["web/ios/App.swift", "mobile-app"],
  ["web/android/app/src/main/MainActivity.kt", "android-app"],
  ["web/electron/main.ts", "desktop-app"],
  ["omnigent/server/api.py", "server"],
  ["omnigent/server/auth.py", "auth"],
];
for (const [fn, key] of cases) {
  const m = resolve(fn);
  assert(`${fn} -> ${key}`, m && m.key === key, m ? m.key : "(unmatched)");
}

console.log(failures ? `\n${failures} FAILURE(S)` : "\nAll areas.json integrity checks passed.");
process.exitCode = failures ? 1 : 0;
