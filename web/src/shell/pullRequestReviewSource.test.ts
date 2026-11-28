import { viewedFingerprint, viewedMarkCovers } from "@/components/diff-review/viewedFiles";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DiffReviewContent } from "@/components/diff-review/reviewItems";
import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  groupReviewThreads,
  keepUnchangedAnnotations,
  keepUnchangedThreadGroups,
  NO_TEXT_DIFF,
  pullRequestReviewAnnotations,
  pullRequestReviewContents,
  pullRequestReviewFile,
  RENAMED_WITHOUT_CHANGES,
  reviewThreadLocation,
  type ReviewDraftPlacement,
  useReviewThreadGroups,
} from "./pullRequestReviewSource";

// One hunk: old lines 1–3, new lines 1–3, with old line 2 replaced.
const PATCH = [
  "diff --git a/src/app.py b/src/app.py",
  "--- a/src/app.py",
  "+++ b/src/app.py",
  "@@ -1,3 +1,3 @@",
  " first",
  "-old",
  "+new",
  " last",
  "",
].join("\n");
const appDiff = parsePatchFiles(PATCH)[0]!.files[0]!;
const svgDiff = parsePatchFiles(
  [
    "diff --git a/icon.svg b/icon.svg",
    "--- a/icon.svg",
    "+++ b/icon.svg",
    "@@ -1 +1 @@",
    '-<svg width="16"/>',
    '+<svg width="24"/>',
    "",
  ].join("\n"),
)[0]!.files[0]!;
const binaryDiff = (path: string, newBlob = "2222222") =>
  parsePatchFiles(
    [
      `diff --git a/${path} b/${path}`,
      `index 1111111..${newBlob} 100644`,
      `Binary files a/${path} and b/${path} differ`,
      "",
    ].join("\n"),
  )[0]!.files[0]!;

function changed(
  path: string,
  status: PullRequestChangedFile["status"] = "modified",
): PullRequestChangedFile {
  return {
    path,
    name: path,
    bytes: 1,
    modified_at: null,
    status,
    lines_added: 1,
    lines_removed: 1,
  } as PullRequestChangedFile;
}

function thread(id: string, overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    path: "src/app.py",
    side: "RIGHT",
    line: 2,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "",
    comments: { data: [], has_more: false, next_cursor: null },
    ...overrides,
  };
}

const ready = (fileDiff: FileDiffMetadata): DiffReviewContent => ({ status: "ready", fileDiff });
const contents = new Map<string, DiffReviewContent>([["src/app.py", ready(appDiff)]]);
const NONE = new Map();

function annotate(
  threads: ReviewThread[],
  options: {
    matchingRevision?: boolean;
    drafts?: Map<string, ReviewDraftPlacement>;
    selectionErrors?: Map<string, string>;
    shown?: ReadonlyMap<string, DiffReviewContent>;
  } = {},
) {
  return pullRequestReviewAnnotations({
    threadsByFile: new Map([["src/app.py", threads]]),
    contents: options.shown ?? contents,
    matchingRevision: options.matchingRevision ?? true,
    drafts: options.drafts ?? NONE,
    selectionErrors: options.selectionErrors ?? NONE,
  });
}

const rows = (result: ReturnType<typeof annotate>) =>
  (result.annotations.get("src/app.py") ?? []).map(({ side, lineNumber, metadata }) => ({
    side,
    lineNumber,
    threads: metadata.threads.map((item) => item.id),
    fileLevel: metadata.fileLevel,
    newThread: metadata.newThread,
    selectionError: metadata.selectionError,
  }));

describe("pull request review files and contents", () => {
  it("lists changed files as the review's files, renames as modified", () => {
    expect(pullRequestReviewFile(changed("a.py", "created"), appDiff).change).toBe("added");
    expect(pullRequestReviewFile(changed("a.py", "deleted"), appDiff).change).toBe("deleted");
    expect(pullRequestReviewFile(changed("a.py", "renamed"), appDiff).change).toBe("modified");
  });

  it("names a rename's previous path from the list, else from the patch, and none otherwise", () => {
    const renamedPatch = parsePatchFiles(
      [
        "diff --git a/src/old.py b/src/new.py",
        "similarity index 90%",
        "rename from src/old.py",
        "rename to src/new.py",
        "--- a/src/old.py",
        "+++ b/src/new.py",
        "@@ -1 +1 @@",
        "-a",
        "+b",
        "",
      ].join("\n"),
    )[0]!.files[0]!;
    const renamed = changed("src/new.py", "renamed");
    expect(
      pullRequestReviewFile({ ...renamed, previous_path: "src/listed.py" }, renamedPatch)
        .previousPath,
    ).toBe("src/listed.py");
    expect(pullRequestReviewFile(renamed, renamedPatch).previousPath).toBe("src/old.py");
    expect(pullRequestReviewFile(changed("a.py"), appDiff).previousPath).toBeNull();
    expect(
      pullRequestReviewFile({ ...changed("a.py"), previous_path: "a.py" }, appDiff).previousPath,
    ).toBeNull();
  });

  it("calls a file binary by its patch, and by its name only when the patch has none", () => {
    const binary = binaryDiff("logo.png");
    expect(binary.hunks).toEqual([]);
    expect(pullRequestReviewFile(changed("logo.png"), binary).isBinary).toBe(true);
    expect(pullRequestReviewFile(changed("logo.png"), undefined).isBinary).toBe(true);
    expect(pullRequestReviewFile(changed("icon.svg"), svgDiff).isBinary).toBe(false);
    expect(pullRequestReviewFile(changed("src/app.py"), undefined).isBinary).toBe(false);
  });

  it("shows each file's parsed diff, or why the patch has none", () => {
    const renamed = { ...appDiff, name: "moved.py", type: "rename-pure" as const };
    const result = pullRequestReviewContents(
      [changed("src/app.py"), changed("moved.py", "renamed"), changed("logo.png")],
      new Map([
        ["src/app.py", appDiff],
        ["moved.py", renamed],
      ]),
    );
    expect(result.get("src/app.py")).toEqual({ status: "ready", fileDiff: appDiff });
    expect(result.get("moved.py")).toMatchObject({
      status: "unavailable",
      message: RENAMED_WITHOUT_CHANGES,
    });
    expect(result.get("logo.png")).toEqual({ status: "unavailable", message: NO_TEXT_DIFF });
    const parsed = pullRequestReviewContents(
      [changed("logo.png"), changed("icon.svg")],
      new Map([
        ["logo.png", binaryDiff("logo.png")],
        ["icon.svg", svgDiff],
      ]),
    );
    expect(parsed.get("logo.png")).toEqual({
      status: "unavailable",
      message: NO_TEXT_DIFF,
      revision: "1111111..2222222",
    });
    expect(parsed.get("icon.svg")).toEqual({ status: "ready", fileDiff: svgDiff });
  });

  it("names a binary file's blobs, so replacing it reads as a new revision", () => {
    const viewedAt = (newBlob: string) => {
      const diff = binaryDiff("image.png", newBlob);
      const file = pullRequestReviewFile(changed("image.png"), diff);
      const imageContents = pullRequestReviewContents(
        [changed("image.png")],
        new Map([["image.png", diff]]),
      );
      return viewedFingerprint(file, imageContents.get("image.png"));
    };
    const marked = viewedAt("2222222");
    expect(marked).not.toBeNull();
    expect(viewedMarkCovers(marked!, viewedAt("2222222"))).toBe(true);
    expect(viewedMarkCovers(marked!, viewedAt("3333333"))).toBe(false);
  });

  it("groups conversations by file, matching a renamed file's old path", () => {
    const moved = { ...appDiff, name: "src/new.py", prevName: "src/old.py" };
    const { byFile, elsewhere } = groupReviewThreads(
      [
        thread("on-app"),
        thread("on-old-path", { path: "src/old.py" }),
        thread("gone", { path: "x.py" }),
      ],
      [changed("src/app.py"), changed("src/new.py", "renamed")],
      new Map([["src/new.py", moved]]),
    );
    expect(byFile.get("src/app.py")?.map((item) => item.id)).toEqual(["on-app"]);
    expect(byFile.get("src/new.py")?.map((item) => item.id)).toEqual(["on-old-path"]);
    expect(elsewhere.map((item) => item.id)).toEqual(["gone"]);
  });
});

describe("pull request review contents before the patch", () => {
  const files = [changed("src/app.py"), changed("logo.png")];

  it("shows every listed file loading while the patch is read", () => {
    const shown = pullRequestReviewContents(files, new Map(), { status: "loading" });
    expect([...shown.values()]).toEqual([{ status: "loading" }, { status: "loading" }]);
  });

  it("says on every file why the patch failed", () => {
    const shown = pullRequestReviewContents(files, new Map(), {
      status: "failed",
      message: "GitHub didn't answer in time.",
    });
    expect([...shown.values()]).toEqual([
      { status: "failed", message: "GitHub didn't answer in time." },
      { status: "failed", message: "GitHub didn't answer in time." },
    ]);
  });

  it("reads the parsed patch once it's in, as without a state", () => {
    const byPath = new Map([["src/app.py", appDiff]]);
    expect(pullRequestReviewContents(files, byPath, { status: "ready" })).toEqual(
      pullRequestReviewContents(files, byPath),
    );
  });
});

describe("keepUnchangedThreadGroups", () => {
  const onApp = thread("on-app");
  const onOther = thread("on-other", { path: "src/other.py" });
  const away = thread("away", { path: "x.py" });
  const groups = (app: ReviewThread[], other: ReviewThread[], elsewhere: ReviewThread[]) => ({
    byFile: new Map([
      ["src/app.py", app],
      ["src/other.py", other],
    ]),
    elsewhere,
  });

  it("keeps the previous groups when new lists hold the same threads", () => {
    const previous = groups([onApp], [onOther], [away]);
    expect(keepUnchangedThreadGroups(previous, groups([onApp], [onOther], [away]))).toBe(previous);
  });

  it("renews only the file whose threads changed, leaving the others as they were", () => {
    const previous = groups([onApp], [onOther], [away]);
    const replied = { ...onApp, comments: { ...onApp.comments } };
    const next = keepUnchangedThreadGroups(previous, groups([replied], [onOther], [away]));
    expect(next).not.toBe(previous);
    expect(next.byFile.get("src/app.py")).toEqual([replied]);
    expect(next.byFile.get("src/other.py")).toBe(previous.byFile.get("src/other.py"));
    expect(next.elsewhere).toBe(previous.elsewhere);
  });

  it("compares threads by object, not id, and notices order, added, removed and moved ones", () => {
    const previous = groups([onApp], [onOther], [away]);
    const sameId = thread("on-app");
    expect(keepUnchangedThreadGroups(previous, groups([sameId], [onOther], [away]))).not.toBe(
      previous,
    );
    const two = groups([onApp, away], [onOther], []);
    expect(keepUnchangedThreadGroups(two, groups([away, onApp], [onOther], []))).not.toBe(two);
    const moved = keepUnchangedThreadGroups(previous, groups([onApp], [onOther], []));
    expect(moved.elsewhere).toEqual([]);
    expect(moved.byFile.get("src/app.py")).toBe(previous.byFile.get("src/app.py"));
    const fewer = keepUnchangedThreadGroups(previous, {
      byFile: new Map([["src/app.py", [onApp]]]),
      elsewhere: [away],
    });
    expect([...fewer.byFile.keys()]).toEqual(["src/app.py"]);
  });

  it("starts from the new groups when there were none", () => {
    const next = groups([onApp], [onOther], [away]);
    expect(keepUnchangedThreadGroups(null, next)).toEqual(next);
  });
});

describe("useReviewThreadGroups", () => {
  const files = [changed("src/app.py"), changed("src/other.py")];
  const byPath = new Map([["src/app.py", appDiff]]);
  const onApp = thread("on-app");
  const onOther = thread("on-other", { path: "src/other.py" });

  it("keeps every file's list across renders, and new lists of the same threads", () => {
    const { result, rerender } = renderHook(
      ({ threads }) => useReviewThreadGroups(threads, files, byPath),
      { initialProps: { threads: [onApp, onOther] } },
    );
    const first = result.current;
    rerender({ threads: [onApp, onOther] });
    expect(result.current).toBe(first);
    expect(result.current.byFile.get("src/app.py")).toBe(first.byFile.get("src/app.py"));
  });

  it("gives a file new conversations when one of its threads changes", () => {
    const { result, rerender } = renderHook(
      ({ threads }) => useReviewThreadGroups(threads, files, byPath),
      { initialProps: { threads: [onApp, onOther] } },
    );
    const first = result.current;
    const replied = { ...onOther, comments: { ...onOther.comments } };
    rerender({ threads: [onApp, replied] });
    expect(result.current.byFile.get("src/other.py")).toEqual([replied]);
    expect(result.current.byFile.get("src/app.py")).toBe(first.byFile.get("src/app.py"));
  });
});

describe("pullRequestReviewAnnotations", () => {
  it("puts conversations on their line and side, sharing a row when they share a line", () => {
    const result = annotate([
      thread("new-side"),
      thread("same-line"),
      thread("old-side", { side: "LEFT", line: 2 }),
    ]);
    expect(rows(result)).toEqual([
      {
        side: "additions",
        lineNumber: 2,
        threads: ["new-side", "same-line"],
        fileLevel: false,
        newThread: false,
        selectionError: undefined,
      },
      {
        side: "deletions",
        lineNumber: 2,
        threads: ["old-side"],
        fileLevel: false,
        newThread: false,
        selectionError: undefined,
      },
    ]);
  });

  it("puts conversations no line shows above the file's first hunk", () => {
    const result = annotate([
      thread("outdated", { outdated: true }),
      thread("file", { subject_type: "FILE", line: null }),
      thread("outside", { line: 40 }),
      thread("placed"),
    ]);
    expect(rows(result)[0]).toEqual({
      side: "additions",
      lineNumber: 0,
      threads: ["outdated", "file", "outside"],
      fileLevel: true,
      newThread: false,
      selectionError: undefined,
    });
    expect(rows(result)[1]?.threads).toEqual(["placed"]);
  });

  it("keeps every conversation above the file when they were read at another revision", () => {
    const result = annotate([thread("placed")], { matchingRevision: false });
    expect(rows(result).map(({ lineNumber, fileLevel }) => [lineNumber, fileLevel])).toEqual([
      [0, true],
    ]);
  });

  it("opens a draft on its line, beside the conversations already there", () => {
    const anchor = { path: "src/app.py", side: "RIGHT" as const, line: 2, start_line: null };
    const onThread = annotate([thread("placed")], {
      drafts: new Map([
        ["src/app.py", { anchor: { ...anchor, head_sha: "h", base_sha: "b" }, stale: false }],
      ]),
    });
    expect(rows(onThread)).toEqual([
      expect.objectContaining({ lineNumber: 2, threads: ["placed"], newThread: true }),
    ]);
    const onOwnLine = annotate([], {
      drafts: new Map([
        [
          "src/app.py",
          { anchor: { ...anchor, side: "LEFT", head_sha: "h", base_sha: "b" }, stale: false },
        ],
      ]),
    });
    expect(rows(onOwnLine)).toEqual([
      expect.objectContaining({ side: "deletions", lineNumber: 2, threads: [], newThread: true }),
    ]);
  });

  it("keeps a draft from an earlier revision, and a selection problem, above the file", () => {
    const anchor = {
      path: "src/app.py",
      side: "RIGHT" as const,
      line: 2,
      start_line: null,
      head_sha: "old",
      base_sha: "b",
    };
    const result = annotate([], {
      drafts: new Map([["src/app.py", { anchor, stale: true }]]),
      selectionErrors: new Map([["src/app.py", "Select lines within one diff hunk."]]),
    });
    expect(rows(result)).toEqual([
      {
        side: "additions",
        lineNumber: 0,
        threads: [],
        fileLevel: true,
        newThread: true,
        selectionError: "Select lines within one diff hunk.",
      },
    ]);
  });

  it("places a loading file's conversations nowhere yet, and a failed one's after the files", () => {
    const loading = annotate([thread("waiting")], {
      shown: new Map([["src/app.py", { status: "loading" }]]),
    });
    expect(loading.annotations.size).toBe(0);
    expect(loading.withoutDiff).toEqual([]);
    const failed = annotate([thread("failed")], {
      shown: new Map([["src/app.py", { status: "failed", message: "No answer." }]]),
    });
    expect(failed.withoutDiff.map((item) => item.id)).toEqual(["failed"]);
  });

  it("hands back the conversations of a file with no diff to show", () => {
    const result = annotate([thread("on-binary")], {
      shown: new Map([["src/app.py", { status: "unavailable", message: NO_TEXT_DIFF }]]),
    });
    expect(result.annotations.size).toBe(0);
    expect(result.withoutDiff.map((item) => item.id)).toEqual(["on-binary"]);
    expect(result.draftsWithoutDiff).toEqual([]);
  });

  it("hands back the draft of a file with no diff to open it in", () => {
    const anchor = {
      path: "src/app.py",
      side: "RIGHT" as const,
      line: 2,
      start_line: null,
      head_sha: "old",
      base_sha: "b",
    };
    const result = annotate([], {
      drafts: new Map([["src/app.py", { anchor, stale: true }]]),
      shown: new Map([["src/app.py", { status: "unavailable", message: NO_TEXT_DIFF }]]),
    });
    expect(result.annotations.size).toBe(0);
    expect(result.draftsWithoutDiff).toEqual(["src/app.py"]);
  });
});

describe("keepUnchangedAnnotations", () => {
  it("keeps a file's rows while its conversations are the same, and replaces them when they change", () => {
    const placed = thread("placed");
    const first = annotate([placed]).annotations;
    const kept = keepUnchangedAnnotations(first, annotate([placed]).annotations);
    expect(kept).toBe(first);
    const changedRows = keepUnchangedAnnotations(
      first,
      annotate([placed, thread("new")]).annotations,
    );
    expect(changedRows).not.toBe(first);
    expect(changedRows.get("src/app.py")).not.toBe(first.get("src/app.py"));
  });
});

describe("reviewThreadLocation", () => {
  it("finds a conversation's line, or the first hunk for one above the file", () => {
    const { annotations } = annotate([
      thread("old-side", { side: "LEFT", line: 2 }),
      thread("outdated", { outdated: true }),
    ]);
    expect(reviewThreadLocation(annotations, contents, "old-side")).toEqual({
      path: "src/app.py",
      line: 2,
      side: "deletions",
      fileLevel: false,
    });
    expect(reviewThreadLocation(annotations, contents, "outdated")).toEqual({
      path: "src/app.py",
      line: 1,
      side: "additions",
      fileLevel: true,
    });
    expect(reviewThreadLocation(annotations, contents, "missing")).toBeUndefined();
  });

  it("uses the old side for a conversation above a deleted file", () => {
    const deleted = parsePatchFiles(
      [
        "diff --git a/src/app.py b/src/app.py",
        "deleted file mode 100644",
        "--- a/src/app.py",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-first",
        "-second",
        "",
      ].join("\n"),
    )[0]!.files[0]!;
    const shown = new Map([["src/app.py", ready(deleted)]]);
    const { annotations } = annotate([thread("file", { subject_type: "FILE", line: null })], {
      shown,
    });
    // A deleted file renders no new side, so the row above it sits on the old side.
    expect(annotations.get("src/app.py")?.[0]).toMatchObject({ side: "deletions", lineNumber: 0 });
    expect(reviewThreadLocation(annotations, shown, "file")).toEqual({
      path: "src/app.py",
      line: 1,
      side: "deletions",
      fileLevel: true,
    });
  });
});
