// Every key of a PR read is built by `pullRequestRecord`, so a shared record's reader and epoch
// are never left out of a key, and no write patches a record by a session's prefix.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { PULL_REQUEST_READ_FAMILIES, PULL_REQUEST_SUMMARY_FAMILY } from "./pullRequestRecord";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OWNER = path.join(SRC, "lib/pullRequestRecord.ts");

function productionFiles(): string[] {
  return fs
    .readdirSync(SRC, { recursive: true, encoding: "utf8" })
    .filter((file) => /\.(ts|tsx)$/.test(file) && !/\.(test|stories)\.tsx?$/.test(file))
    .filter((file) => !file.endsWith(".d.ts") && !file.split(path.sep).includes("test"))
    .map((file) => path.join(SRC, file))
    .filter((file) => file !== OWNER);
}

const withoutComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

const families = [...PULL_REQUEST_READ_FAMILIES, PULL_REQUEST_SUMMARY_FAMILY].join("|");
// `[family, <not another family name>, <more>`: a key past its session, the URL included. A key of
// a session's own list (`[family, session]`) and a list of families, the classic view's included,
// aren't keys of a PR read.
const HAND_BUILT_KEY = new RegExp(
  `\\[\\s*"(${families})"\\s*,\\s*(?!"(?:classic-)?github-)[^\\]\\s][^\\]]*?,`,
  "g",
);

describe("PR read keys", () => {
  it("are built only by pullRequestRecord", () => {
    const found = productionFiles().flatMap((file) => {
      const source = withoutComments(fs.readFileSync(file, "utf8"));
      return [...source.matchAll(HAND_BUILT_KEY)].map(
        (match) => `${path.relative(SRC, file)}: ${match[0].replace(/\s+/g, " ")}`,
      );
    });
    expect(found).toEqual([]);
  });

  it("would catch a hand-built summary key", () => {
    expect(
      `client.getQueryData(["pr-summary", "pr", prUrl]);`.match(HAND_BUILT_KEY),
    ).not.toBeNull();
  });

  it("would catch a hand-built key", () => {
    const sample = `queryClient.invalidateQueries({ queryKey: ["github-info", session, prUrl] });`;
    expect(sample.match(HAND_BUILT_KEY)).not.toBeNull();
    expect(`useQuery({ queryKey: ["github-info", session] })`.match(HAND_BUILT_KEY)).toBeNull();
    expect(
      `const roots = ["github-info", "github-pr-diff", "x"];`.match(HAND_BUILT_KEY),
    ).toBeNull();
    expect(
      `const roots = new Set(["github-info", "classic-github-info", "x"]);`.match(HAND_BUILT_KEY),
    ).toBeNull();
  });
});

// Each list read publishes its own answer's rows; nothing else may write a PR's summary, so a
// saved list, a list on screen or a read with no proven reader never fills one.
const SUMMARY_PUBLISHERS = ["hooks/usePullRequestListStats.ts"];

describe("PR summaries", () => {
  it("are published only by the list stats read, from its own answers", () => {
    const callers = productionFiles()
      .filter((file) =>
        /publishPullRequestSummaries\(/.test(withoutComments(fs.readFileSync(file, "utf8"))),
      )
      .map((file) => path.relative(SRC, file))
      .sort();
    expect(callers).toEqual(SUMMARY_PUBLISHERS);
  });

  it("are only read by name elsewhere, never written", () => {
    const found = productionFiles().flatMap((file) => {
      const source = withoutComments(fs.readFileSync(file, "utf8"));
      if (!SUMMARY_KEY_NAMES.test(source)) return [];
      const relative = path.relative(SRC, file);
      if (!SUMMARY_READERS.includes(relative)) return [`${relative}: names a summary key`];
      return CACHE_WRITES.test(source) ? [`${relative}: writes the cache`] : [];
    });
    expect(found).toEqual([]);
  });

  it("would catch a direct write to a summary key", () => {
    const sample = `client.setQueryData(pullRequestSummaryKey(url, owner), summary);`;
    expect(SUMMARY_KEY_NAMES.test(sample) && CACHE_WRITES.test(sample)).toBe(true);
  });
});

// Where a summary's key is named outside its owner: the session panel's preview, which only reads.
const SUMMARY_READERS = ["hooks/usePullRequestSummaryPreview.ts"];
const SUMMARY_KEY_NAMES = /pullRequestSummaryKey|PULL_REQUEST_SUMMARY_FAMILY|"pr-summary"/;
const CACHE_WRITES =
  /\b(setQueryData|setQueriesData|setQueryDefaults|removeQueries|resetQueries)\b/;
