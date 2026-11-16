// The shapes here come from Claude Code and Codex replies and their system
// prompts' file-reference rules; see the header of fileReferences.ts.

import { describe, expect, it } from "vitest";
import {
  findProseFileReferences,
  mergeProseExtents,
  parseFileReference,
  splitFileCitation,
  trailingLineNote,
  worthSearching,
} from "./fileReferences";

describe("splitFileCitation", () => {
  it.each([
    ["a.ts:12", { path: "a.ts", line: 12 }],
    ["a.ts:12:5", { path: "a.ts", line: 12, column: 5 }],
    ["a.ts:12-18", { path: "a.ts", line: 12, endLine: 18 }],
    ["a.ts:104–106", { path: "a.ts", line: 104, endLine: 106 }],
    ["a.ts:580,1116,1863", { path: "a.ts", line: 580 }],
    ["a.ts#L12C5", { path: "a.ts", line: 12, column: 5 }],
    ["a.ts#L12-L18", { path: "a.ts", line: 12, endLine: 18 }],
  ])("splits %s", (text, expected) => {
    expect(splitFileCitation(text)).toEqual({ ...expected, hasPosition: true });
  });
});

describe("parseFileReference in inline code", () => {
  it.each([
    ["src/app.ts:42", ["src/app.ts"]],
    ["b/server/index.js#L10", ["b/server/index.js", "server/index.js"]],
    ["~/.config/omnigent/settings.json", ["~/.config/omnigent/settings.json"]],
    ["./docs/design.md", ["docs/design.md"]],
    ["../notes/log.txt", ["../notes/log.txt"]],
    ["/etc/hosts", ["/etc/hosts"]],
    ["cli_auth.py:753-755", ["cli_auth.py"]],
    ["Makefile:12", ["Makefile"]],
    ["docs/", ["docs"]],
    ["devtools/ai", ["devtools/ai"]],
    ["Architecture & Migration/plan.md", ["Architecture & Migration/plan.md"]],
    [
      "accounts/Example (EMEA)/Predictive Optimization/notes.md",
      ["accounts/Example (EMEA)/Predictive Optimization/notes.md"],
    ],
    [
      '"build/mac/Omnigent Dev.app/Contents/MacOS/Omnigent Dev"',
      ["build/mac/Omnigent Dev.app/Contents/MacOS/Omnigent Dev"],
    ],
    ["file:///home/u/ws/My%20Notes.md#L3", ["/home/u/ws/My Notes.md"]],
  ])("reads %s", (text, candidates) => {
    expect(parseFileReference(text, "code")?.candidates).toEqual(candidates);
  });

  it("marks a trailing slash as naming a folder", () => {
    expect(parseFileReference("~/.cache/omnigent/", "code")?.folderHint).toBe(true);
    expect(parseFileReference("src/app.ts", "code")?.folderHint).toBe(false);
  });

  it.each([
    "git status",
    "useState",
    "npm run build",
    "rg pattern -n src",
    "https://example.com/a.md",
    "omnigent/stores/*/sqlalchemy_store.py",
    "~/worktrees/omnigent-<branch>",
    "agentbricks/mas/third_party/omnigent/...",
    "~/.cache/omni-fast-venv-…",
    "/v1/sessions/{id}",
    "$HOME/notes.md",
    "C:\\repo\\project\\main.rs:12:5",
    "a / b",
    "docs/notes.md#heading",
    "docs/page.md?raw=1",
    "v1.2",
    "~",
  ])("rejects %s", (text) => {
    expect(parseFileReference(text, "code")).toBeNull();
  });
});

describe("findProseFileReferences", () => {
  const found = (text: string) =>
    findProseFileReferences(text).map(({ start, extents }) => text.slice(start, extents[0].end));

  it.each([
    ["Wrote ~/ws/notes.md, then left.", ["~/ws/notes.md"]],
    ["Failing at (managed_hosts.py:1421) now", ["managed_hosts.py:1421"]],
    ["I edited src/app.ts:12.", ["src/app.ts:12"]],
    ['"absolute_file_path": "/home/u/ws/a.tsx",', ["/home/u/ws/a.tsx"]],
    ["see /home/u/universe/experimental/data for it", ["/home/u/universe/experimental/data"]],
    ["the tmp/out/ folder", ["tmp/out/"]],
    ["read README.md first", ["README.md"]],
  ])("finds the reference in %s", (text, expected) => {
    expect(found(text)).toEqual(expected);
  });

  it.each([
    "pub/sub and ALB/NLB, 1/2 of the time",
    "run /merge or /health",
    "see https://example.com/docs/a.md",
    "v1.2 and 3.14",
  ])("finds nothing in %s", (text) => {
    expect(found(text)).toEqual([]);
  });

  it("offers each way an anchored path could run on through spaces, longest first", () => {
    const text = "Saved /home/u/ws/My Notes/plan final.md for review.";
    const [reference] = findProseFileReferences(text);
    expect(reference.extents.map(({ end }) => text.slice(reference.start, end))).toEqual([
      "/home/u/ws/My Notes/plan final.md",
      "/home/u/ws/My",
    ]);
  });

  it("merges the extents into candidates that keep their own position and length", () => {
    const text = "./My Notes.md:3 and";
    const merged = mergeProseExtents(text, [15, 4]);
    expect(merged?.reference.candidates).toEqual(["My Notes.md"]);
    expect(merged?.candidateLengths).toEqual([15]);
    expect(merged?.reference.candidateCitations?.[0].line).toBe(3);
  });
});

describe("trailingLineNote", () => {
  it.each([
    [" (lines 104–106) more", { line: 104, endLine: 106 }],
    [" (line 7)", { line: 7 }],
    [" (L12-L20)", { line: 12, endLine: 20 }],
  ])("reads %s", (text, expected) => {
    expect(trailingLineNote(text)).toEqual(expected);
  });

  it("ignores other parentheticals", () => {
    expect(trailingLineNote(" (see below)")).toBeNull();
  });
});

describe("worthSearching", () => {
  it.each([
    ["cli_auth.py:753", "code", true],
    ["checks/src/lib.rs", "code", true],
    ["README.md", "code", true],
    ["process.env", "code", false],
    ["process.env.HOME", "code", false],
    ["/abs/x.ts", "code", false],
    ["~/x.ts", "code", false],
    ["docs/", "code", false],
  ] as const)("%s in %s → %s", (text, source, expected) => {
    const reference = parseFileReference(text, source);
    expect(reference && worthSearching(reference)).toBe(expected);
  });
});
