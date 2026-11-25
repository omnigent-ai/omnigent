import { parseDiffFromFile } from "@pierre/diffs";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createDiffRenderFailureReporter,
  diffRenderFailureReportFor,
  fileExtension,
} from "./diffRenderFailure";

const fileDiff = parseDiffFromFile(
  { name: "a.ts", contents: "one\ntwo\n" },
  { name: "a.ts", contents: "one\n2\n" },
);
const missingRow = (shortSides: string[]) =>
  Object.assign(new Error("no line"), {
    name: "DiffMissingRowError",
    missingRow: { diffStyle: "split", rowType: "context-expanded", shortSides },
  });

afterEach(() => vi.restoreAllMocks());

describe("fileExtension", () => {
  it("names a short extension, and none or other otherwise", () => {
    expect(fileExtension("src/App.TSX")).toBe("tsx");
    expect(fileExtension("Makefile")).toBe("none");
    expect(fileExtension("config/.env")).toBe("none");
    expect(fileExtension("notes.")).toBe("none");
    expect(fileExtension("a.very-long-extension-name")).toBe("other");
    expect(fileExtension("dir.d/file")).toBe("none");
  });
});

describe("diffRenderFailureReportFor", () => {
  it("reports the view, the file's kind, the short side and the diff's shape, never its path", () => {
    const report = diffRenderFailureReportFor(
      "pull_request",
      "private/secret-name.py",
      fileDiff,
      missingRow(["deletions"]),
    );
    expect(report).toEqual({
      view: "pull_request",
      extension: "py",
      failure: "missing_row",
      shortSide: "deletions",
      rowType: "context-expanded",
      diffStyle: "split",
      partial: false,
      hunks: 1,
      deletionLines: 2,
      additionLines: 2,
    });
    expect(JSON.stringify(report)).not.toContain("secret");
  });

  it("names both sides, or none it knows, and any other error as other", () => {
    const both = diffRenderFailureReportFor(
      "working_copy",
      "a.ts",
      fileDiff,
      missingRow(["deletions", "additions"]),
    );
    expect(both.shortSide).toBe("both");
    expect(
      diffRenderFailureReportFor("working_copy", "a.ts", fileDiff, missingRow([])).shortSide,
    ).toBe("unknown");
    expect(diffRenderFailureReportFor("working_copy", "a.ts", undefined, new Error("x"))).toEqual({
      view: "working_copy",
      extension: "ts",
      failure: "other",
    });
  });
});

describe("createDiffRenderFailureReporter", () => {
  it("logs each failed diff once however often it renders again, without its path", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const report = createDiffRenderFailureReporter(() => "pull_request_commit");
    report("src/secret-name.ts", fileDiff, missingRow(["additions"]));
    report("src/secret-name.ts", fileDiff, missingRow(["additions"]));
    expect(warn).toHaveBeenCalledOnce();
    const [, logged] = warn.mock.calls[0]!;
    expect(logged).toMatchObject({ view: "pull_request_commit", shortSide: "additions" });
    expect(JSON.stringify(logged)).not.toContain("secret-name");
  });

  it("logs a failure without a diff each time, as there's nothing to tell them apart by", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const report = createDiffRenderFailureReporter(() => "working_copy");
    report("a.ts", undefined, new Error("x"));
    report("a.ts", undefined, new Error("x"));
    expect(warn).toHaveBeenCalledTimes(2);
  });
});
