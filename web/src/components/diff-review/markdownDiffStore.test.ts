import { parseDiffFromFile, parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { describe, expect, it, vi } from "vitest";
import { computeMarkdownDiff, type MarkdownDiffResult } from "./markdownDiff";
import type { MarkdownDiffComputer, MarkdownDiffRequest } from "./markdownDiffComputation";
import {
  FULL_TEXT_FAILED_MESSAGE,
  fullTextOf,
  MarkdownDiffStore,
  NO_FULL_TEXT_MESSAGE,
} from "./markdownDiffStore";

const PATH = "README.md";

function fullDiff(before: string | null, after: string | null): FileDiffMetadata {
  return parseDiffFromFile(
    before === null ? null : { name: PATH, contents: before },
    after === null ? null : { name: PATH, contents: after },
    undefined,
    true,
  );
}

function patchDiff(): FileDiffMetadata {
  const patch = [
    `diff --git a/${PATH} b/${PATH}`,
    `--- a/${PATH}`,
    `+++ b/${PATH}`,
    "@@ -1 +1 @@",
    "-Old.",
    "+New.",
    "",
  ].join("\n");
  return parsePatchFiles(patch)[0]!.files[0]!;
}

/** A computer whose diffs finish when the test says so. */
function manualComputer() {
  const calls: {
    request: MarkdownDiffRequest;
    signal?: AbortSignal;
    finish: (result?: MarkdownDiffResult) => void;
    fail: (error: Error) => void;
  }[] = [];
  const computer: MarkdownDiffComputer = {
    compute: (request, signal) =>
      new Promise((resolve, reject) => {
        signal?.addEventListener("abort", () =>
          reject(new DOMException("cancelled", "AbortError")),
        );
        calls.push({
          request,
          signal,
          finish: (result) => resolve(result ?? computeMarkdownDiff(request.before, request.after)),
          fail: reject,
        });
      }),
    dispose: vi.fn(),
  };
  return { computer, calls };
}

/** Lets queued microtasks and one timer turn run, so loads and publishes finish. */
async function settled() {
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
  await Promise.resolve();
}

describe("fullTextOf", () => {
  it("reads both sides of a full-text diff, with null for a missing side", () => {
    expect(fullTextOf(fullDiff("a\n\nb", "a\n"))).toEqual({ before: "a\n\nb", after: "a\n" });
    expect(fullTextOf(fullDiff(null, "new\n"))).toEqual({ before: null, after: "new\n" });
    expect(fullTextOf(fullDiff("gone\n", null))).toEqual({ before: "gone\n", after: null });
  });
});

describe("MarkdownDiffStore", () => {
  it("prepares a requested preview once and reuses it for the same diff", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    const fileDiff = fullDiff("Old.", "New.");
    store.request(PATH, fileDiff);
    store.request(PATH, fileDiff);
    await settled();
    expect(store.states.get(PATH)).toEqual({ status: "loading", previous: undefined });
    expect(calls).toHaveLength(1);
    calls[0]!.finish();
    await settled();
    expect(store.states.get(PATH)).toMatchObject({ status: "ready" });
  });

  it("keeps the shown preview while a refresh loads and ignores the older result", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    store.request(PATH, fullDiff("One.", "Two."));
    await settled();
    calls[0]!.finish();
    await settled();
    const first = store.states.get(PATH);
    store.request(PATH, fullDiff("One.", "Three."));
    await settled();
    store.request(PATH, fullDiff("One.", "Four."));
    await settled();
    expect(calls[1]!.signal?.aborted).toBe(true);
    expect(store.states.get(PATH)).toEqual({
      status: "loading",
      previous: first?.status === "ready" ? first.sections : undefined,
    });
    calls[2]!.finish();
    await settled();
    const latest = store.states.get(PATH);
    expect(latest?.status === "ready" && latest.sections.map((entry) => entry.text)).toEqual([
      "One.",
      "Four.",
    ]);
  });

  it("reuses the prepared preview when a refresh leaves the text unchanged", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    store.request(PATH, fullDiff("Same.", "Text."));
    await settled();
    calls[0]!.finish();
    await settled();
    const prepared = store.states.get(PATH);
    store.request(PATH, fullDiff("Same.", "Text."));
    await settled();
    expect(calls).toHaveLength(1);
    expect(store.states.get(PATH)).toBe(prepared);
  });

  it("shows a failure, keeps the last preview, and prepares it again on retry", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    store.request(PATH, fullDiff("A.", "B."));
    await settled();
    calls[0]!.fail(new Error("The rich diff couldn't be prepared."));
    await settled();
    expect(store.states.get(PATH)).toEqual({
      status: "failed",
      message: "The rich diff couldn't be prepared.",
      previous: undefined,
    });
    store.retry(PATH);
    await settled();
    calls[1]!.finish();
    await settled();
    expect(store.states.get(PATH)).toMatchObject({ status: "ready" });
  });

  it("reports a file beyond the limits as unavailable", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    store.request(PATH, fullDiff("A.", "B."));
    await settled();
    calls[0]!.finish({ status: "tooLarge", message: "Too large." });
    await settled();
    expect(store.states.get(PATH)).toEqual({ status: "unavailable", message: "Too large." });
  });

  it("loads a patch's full text before comparing, and fails when that load fails", async () => {
    const { computer, calls } = manualComputer();
    const loadFullText = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({
        oldFile: { name: PATH, contents: "Old.\n\nKept." },
        newFile: { name: PATH, contents: "New.\n\nKept." },
      });
    const store = new MarkdownDiffStore({ computer, loadFullText });
    const fileDiff = patchDiff();
    store.request(PATH, fileDiff);
    await settled();
    expect(store.states.get(PATH)).toMatchObject({
      status: "failed",
      message: FULL_TEXT_FAILED_MESSAGE,
    });
    store.retry(PATH);
    await settled();
    expect(calls[0]!.request).toEqual({ before: "Old.\n\nKept.", after: "New.\n\nKept." });
  });

  it("can't preview a patch when the review has no way to load full text", async () => {
    const { computer } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    store.request(PATH, patchDiff());
    await settled();
    expect(store.states.get(PATH)).toEqual({
      status: "unavailable",
      message: NO_FULL_TEXT_MESSAGE,
    });
  });

  it("cancels work and stops publishing once disposed", async () => {
    const { computer, calls } = manualComputer();
    const store = new MarkdownDiffStore({ computer });
    const listener = vi.fn();
    store.subscribe(listener);
    store.request(PATH, fullDiff("A.", "B."));
    await settled();
    listener.mockClear();
    store.dispose();
    expect(calls[0]!.signal?.aborted).toBe(true);
    expect(computer.dispose).toHaveBeenCalled();
    store.request(PATH, fullDiff("A.", "C."));
    await settled();
    expect(listener).not.toHaveBeenCalled();
  });
});
