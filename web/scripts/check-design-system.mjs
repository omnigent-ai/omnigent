#!/usr/bin/env node

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(webRoot, "..");

const rules = [
  {
    name: "raw-color",
    pattern: /(?:#[0-9a-f]{3,8}\b|\b(?:rgb|hsl|oklch|oklab)\()/i,
    message: "Use a semantic color token instead of a raw color value.",
  },
  {
    name: "arbitrary-font-size",
    pattern: /\btext-\[(?:\d+(?:\.\d+)?)(?:px|rem)\]/,
    message: "Use a named typography step such as text-13 or text-card-body.",
  },
  {
    name: "literal-radius",
    pattern: /\brounded(?:-[trblxy]{1,2})?-\[(?:\d+(?:\.\d+)?)(?:px|rem)\]/,
    message: "Use an Otto radius token or a canonical component primitive.",
  },
  {
    name: "arbitrary-shadow",
    pattern: /\bshadow-\[(?!var\().+?\]/,
    message: "Use an elevation token instead of an arbitrary shadow.",
  },
  {
    name: "important",
    pattern: /!important|(?:dark:|\s)![a-z][\w-]*(?:-|\[)/,
    message: "Avoid !important and Tailwind important modifiers in feature UI.",
  },
];

const ignoredPaths = [
  /(?:^|\/)__snapshots__\//,
  /\.(?:test|spec)\.[jt]sx?$/,
  /^web\/src\/assets\//,
  /^web\/src\/components\/ai-elements\//,
  /^web\/src\/components\/icons\//,
  /^web\/src\/lib\/(?:syntaxTheme|themePalette)\.ts$/,
];

export function analyzeLine(file, line) {
  if (!/\.[jt]sx?$/.test(file) || ignoredPaths.some((pattern) => pattern.test(file))) return [];
  const trimmed = line.trim();
  if (!trimmed || /^(?:\/\/|\/\*|\*|\*\/)/.test(trimmed)) return [];
  return rules
    .filter(({ pattern }) => pattern.test(line))
    .map(({ name, message }) => ({ file, line, name, message }));
}

function git(args) {
  return execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
}

function refExists(ref) {
  try {
    git(["rev-parse", "--verify", "--quiet", ref]);
    return true;
  } catch {
    return false;
  }
}

function comparisonBase() {
  const requested = process.env.DESIGN_SYSTEM_BASE;
  const candidates = requested ? [requested] : ["upstream/main", "origin/main", "main"];
  const ref = candidates.find(refExists);
  if (!ref) return null;
  return git(["merge-base", "HEAD", ref]).trim();
}

export function parseAddedLines(diff) {
  const findings = [];
  let file = "";
  let lineNumber = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++ b/")) {
      file = line.slice(6);
      continue;
    }
    const hunk = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)/);
    if (hunk) {
      lineNumber = Number(hunk[1]);
      continue;
    }
    if (!file || line.startsWith("---") || line.startsWith("+++")) continue;
    if (line.startsWith("+")) {
      for (const finding of analyzeLine(file, line.slice(1))) {
        findings.push({ ...finding, lineNumber });
      }
      lineNumber += 1;
    } else if (!line.startsWith("-")) {
      lineNumber += 1;
    }
  }
  return findings;
}

function runSelfTest() {
  assert.equal(analyzeLine("web/src/Foo.tsx", 'className="text-[13px]"').length, 1);
  assert.equal(analyzeLine("web/src/Foo.tsx", 'className="text-card-title"').length, 0);
  assert.equal(analyzeLine("web/src/Foo.test.tsx", 'className="text-[13px]"').length, 0);
  assert.equal(analyzeLine("web/src/lib/syntaxTheme.ts", 'foreground: "#fff"').length, 0);
  assert.equal(
    parseAddedLines("+++ b/web/src/Foo.tsx\n@@ -1 +1 @@\n+const x = '#fff';").at(0)?.lineNumber,
    1,
  );
  console.log("Design-system lint self-test passed.");
}

function main() {
  if (process.argv.includes("--self-test")) {
    runSelfTest();
    return;
  }

  const base = comparisonBase();
  const args = ["diff", "--unified=0", "--no-color"];
  if (base) args.push(base);
  else args.push("HEAD");
  args.push("--", "web/src");

  const findings = parseAddedLines(git(args));
  if (findings.length === 0) {
    console.log(`Design-system lint passed${base ? ` against ${base.slice(0, 8)}` : ""}.`);
    return;
  }

  console.error("Design-system lint found new token bypasses:\n");
  for (const finding of findings) {
    console.error(`${finding.file}:${finding.lineNumber} [${finding.name}] ${finding.message}`);
    console.error(`  ${finding.line.trim()}`);
  }
  console.error("\nSee web/docs/design-system.md for approved primitives and exceptions.");
  process.exitCode = 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main();
