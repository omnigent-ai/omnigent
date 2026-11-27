// Every key of a PR read is built by `pullRequestRecord`, so each names its session and PR in
// the same places, and every write patches exactly its own session's entries.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { PULL_REQUEST_READ_FAMILIES } from "./pullRequestRecord";

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

const families = PULL_REQUEST_READ_FAMILIES.join("|");
// `[family, <not another family name>, <more>`: a key past its session, the URL included. A key of
// a session's own list (`[family, session]`) and a list of families, the classic view's included,
// aren't keys of a PR read.
const HAND_BUILT_KEY = new RegExp(
  `\\[\\s*"(${families})"\\s*,\\s*(?!"(?:pr-page-)?github-)[^\\]\\s][^\\]]*?,`,
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

  it("would catch a hand-built key", () => {
    const sample = `queryClient.invalidateQueries({ queryKey: ["pr-page-github-info", session, prUrl] });`;
    expect(sample.match(HAND_BUILT_KEY)).not.toBeNull();
    expect(
      `useQuery({ queryKey: ["pr-page-github-info", session] })`.match(HAND_BUILT_KEY),
    ).toBeNull();
    expect(
      `const roots = ["pr-page-github-info", "pr-page-github-pr-diff", "x"];`.match(HAND_BUILT_KEY),
    ).toBeNull();
    expect(
      `const roots = new Set(["pr-page-github-info", "github-info", "x"]);`.match(HAND_BUILT_KEY),
    ).toBeNull();
  });
});
