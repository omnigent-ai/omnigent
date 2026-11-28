// Two PRs of a stack that change the same file, shown one after the other in one review, with the
// real FileDiff: the second PR's diff shows its own lines, not the first one's.

import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";
import { render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import { patchFileDiffs } from "./patchFileDiffs";

const PATH = "src/settings.tsx";

/** A PR's patch of {@link PATH}: `lines` added after line 2 of a file both PRs share. */
function pullRequestPatch(added: readonly string[]): string {
  return [
    `diff --git a/${PATH} b/${PATH}`,
    `--- a/${PATH}`,
    `+++ b/${PATH}`,
    `@@ -1,3 +1,${3 + added.length} @@`,
    " const first = 1;",
    " const second = 2;",
    ...added.map((line) => `+${line}`),
    " const third = 3;",
    "",
  ].join("\n");
}

const EARLIER = pullRequestPatch(["const earlier = true;"]);
const LATER = pullRequestPatch(["const later = 1;", "const laterStill = 2;", "const last = 3;"]);

const shownText = () => document.querySelector("diffs-container")?.shadowRoot?.textContent ?? "";

function Review({ fileDiff }: { fileDiff: FileDiffMetadata }) {
  return <FileDiff fileDiff={fileDiff} options={{ disableFileHeader: true }} disableWorkerPool />;
}

describe("two PRs' diffs of the same file in one review", () => {
  beforeAll(() => {
    Element.prototype.scrollTo ??= function scrollTo() {};
  });

  it("shows the later PR's own lines after the earlier PR's", async () => {
    const [earlier] = patchFileDiffs(EARLIER);
    const [later] = patchFileDiffs(LATER);
    const view = render(<Review fileDiff={earlier!} />);
    await waitFor(() => expect(shownText()).toContain("const earlier = true;"));
    view.rerender(<Review fileDiff={later!} />);
    await waitFor(() => expect(shownText()).toContain("const laterStill = 2;"));
    expect(shownText()).not.toContain("const earlier = true;");
  });
});

describe("patchFileDiffs", () => {
  it("keys each file by its patch, so another PR's diff of the same path never matches it", () => {
    const [earlier] = patchFileDiffs(EARLIER);
    const [later] = patchFileDiffs(LATER);
    expect(earlier!.name).toBe(later!.name);
    expect(earlier!.cacheKey).toBeDefined();
    expect(earlier!.cacheKey).not.toBe(later!.cacheKey);
  });

  it("gives the same patch the same key, so its highlighting is reused", () => {
    expect(patchFileDiffs(LATER)[0]!.cacheKey).toBe(patchFileDiffs(LATER)[0]!.cacheKey);
  });

  it("keys every file of a patch apart", () => {
    const other = EARLIER.replaceAll(PATH, "src/other.tsx");
    const keys = patchFileDiffs(`${EARLIER}${other}`).map((file) => file.cacheKey);
    expect(keys).toHaveLength(2);
    expect(new Set(keys).size).toBe(2);
  });

  it("keys the same text apart by its scope, such as the file a suggestion shows as", () => {
    expect(patchFileDiffs(LATER, "src/a.ts")[0]!.cacheKey).not.toBe(
      patchFileDiffs(LATER, "src/b.ts")[0]!.cacheKey,
    );
  });

  it("parses as the library does, files and hunks alike", () => {
    const [ours] = patchFileDiffs(LATER);
    const [theirs] = parsePatchFiles(LATER)[0]!.files;
    expect({ ...ours, cacheKey: undefined }).toEqual({ ...theirs, cacheKey: undefined });
  });
});
