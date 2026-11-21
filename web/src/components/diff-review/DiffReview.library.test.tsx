// DiffReview against the real CodeView: expanding hidden context and loading a
// patch's full text happen inside the library, so these don't stub it.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  CodeView,
  hydratePartialDiff,
  parsePatchFiles,
  type CodeViewScrollTarget,
  type FileDiffMetadata,
} from "@pierre/diffs";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DiffReview, type DiffReviewProps } from "./DiffReview";
import { reviewFile } from "./diffReviewModel";
import { computeFileDiff } from "./fileDiffComputation";
import { sideRow } from "./renderedRows";
import type { ReviewRevealTarget } from "./reviewNavigation";

const PATH = "src/long.ts";
const numbered = (count: number) =>
  Array.from({ length: count }, (_, index) => `line ${index + 1}`);

function reviewOf(fileDiff: FileDiffMetadata, overrides: Partial<DiffReviewProps> = {}) {
  return (
    <DiffReview
      diagnosticsView="working_copy"
      files={[reviewFile(PATH, "modified")]}
      contents={new Map([[PATH, { status: "ready", fileDiff }]])}
      onRetry={() => undefined}
      layout="unified"
      wrapLines={false}
      findOpen={false}
      findFocusRequest={0}
      onFindClose={() => undefined}
      emptyMessage="No changes"
      {...overrides}
    />
  );
}

/** The rendered file's shadow root, once CodeView has rendered it. */
function renderedFile(): ShadowRoot {
  const root = document.querySelector("diffs-container")?.shadowRoot;
  if (!root) throw new Error("The file isn't rendered yet.");
  return root;
}

const measure = Element.prototype.getBoundingClientRect;

describe("DiffReview with the real CodeView", () => {
  // jsdom can't scroll; CodeView still lays out and renders what a scroll asks for.
  beforeAll(() => {
    Element.prototype.scrollTo ??= function scrollTo() {};
    Element.prototype.scrollBy ??= function scrollBy() {};
    // Nor lay out: the review's view gets a window's height, so what CodeView renders follows
    // from it rather than from its overscroll past a view with none.
    Element.prototype.getBoundingClientRect = function getBoundingClientRect(this: Element) {
      const bounds = measure.call(this);
      if (!this.classList.contains("diff-review-scroll") || bounds.height > 0) return bounds;
      return new DOMRect(bounds.x, bounds.y, bounds.width, 600);
    };
  });
  afterAll(() => {
    Element.prototype.getBoundingClientRect = measure;
  });

  describe("a file the library can't draw", () => {
    const patch = [
      `diff --git a/${PATH} b/${PATH}`,
      "index 1111111..2222222 100644",
      `--- a/${PATH}`,
      `+++ b/${PATH}`,
      "@@ -294,3 +294,3 @@",
      " line 294",
      "-line 295",
      "+line 295 changed",
      " line 296",
      "",
    ].join("\n");
    // Text shorter than the patch says, as a side read from another revision or not at all.
    const short = {
      oldFile: { name: PATH, contents: "line 1\nline 2\n" },
      newFile: { name: PATH, contents: "" },
    };
    const errorWrappers = () =>
      [...document.querySelectorAll("diffs-container")].filter((host) =>
        host.shadowRoot?.querySelector("[data-error-wrapper]"),
      );
    let reports: unknown[];
    beforeEach(() => {
      reports = [];
      vi.spyOn(console, "warn").mockImplementation((message: unknown, report: unknown) => {
        if (message === "A diff couldn't be drawn") reports.push(report);
      });
    });
    afterEach(() => {
      vi.mocked(console.warn).mockRestore();
    });

    it("shows a note in place of its diff and reports its shape once", async () => {
      const broken = hydratePartialDiff("clone", parsePatchFiles(patch)[0]!.files[0]!, short);
      render(reviewOf(broken, { diagnosticsView: "pull_request" }));
      await waitFor(() =>
        expect(screen.getByTestId("diff-review-file-status")).toHaveAttribute(
          "title",
          "Couldn’t show this file’s diff. Open the file to read it.",
        ),
      );
      expect(errorWrappers()).toEqual([]);
      expect(reports).toEqual([
        expect.objectContaining({
          view: "pull_request",
          extension: "ts",
          failure: "missing_row",
          shortSide: "both",
          partial: false,
          hunks: 1,
          deletionLines: 2,
          additionLines: 0,
        }),
      ]);
    });

    it("keeps a patch's own lines when its full text doesn't match", async () => {
      const fileDiff = parsePatchFiles(patch)[0]!.files[0]!;
      const source = { expandContext: { load: async () => short, lineCount: 10 } };
      render(reviewOf(fileDiff, { source, diagnosticsView: "pull_request" }));
      await waitFor(() =>
        expect(renderedFile().querySelector("[data-expand-all-button]")).not.toBeNull(),
      );
      await act(async () => {
        fireEvent.click(renderedFile().querySelector("[data-expand-all-button]")!);
      });
      await waitFor(() => expect(screen.getByText("Couldn’t load the full file")).toBeVisible());
      expect(renderedFile().textContent).toContain("line 295 changed");
      expect(errorWrappers()).toEqual([]);
      expect(reports).toEqual([]);
    });
  });

  it("expands hidden context to reveal an unchanged line by its old line number", async () => {
    // Line 2 is deleted, so unchanged old line 100 shows as current line 99.
    const before = `${numbered(300).join("\n")}\n`;
    const after = `${numbered(300)
      .filter((line) => line !== "line 2")
      .map((line) => (line === "line 295" ? "line 295 changed" : line))
      .join("\n")}\n`;
    const fileDiff = computeFileDiff({
      path: PATH,
      before,
      after,
      ignoreWhitespace: false,
      revision: "1",
    });
    const view = render(reviewOf(fileDiff, { revealPath: null }));
    await waitFor(() => expect(renderedFile().querySelector("[data-line]")).not.toBeNull());
    expect(sideRow(renderedFile(), 100, "deletions")).toBeUndefined();

    const target: ReviewRevealTarget = {
      path: PATH,
      line: 100,
      side: "deletions",
      align: "center",
    };
    view.rerender(reviewOf(fileDiff, { revealPath: null, revealTarget: target }));
    await waitFor(() => expect(sideRow(renderedFile(), 100, "deletions")).toBeDefined());
    expect(sideRow(renderedFile(), 100, "deletions")?.getAttribute("data-line")).toBe("99");
  });

  it("finds text in context a patch left out once expanding loads the full file", async () => {
    const patch = [
      `diff --git a/${PATH} b/${PATH}`,
      "index 1111111..2222222 100644",
      `--- a/${PATH}`,
      `+++ b/${PATH}`,
      "@@ -294,3 +294,3 @@",
      " line 294",
      "-line 295",
      "+line 295 changed",
      " line 296",
      "",
    ].join("\n");
    const fileDiff = parsePatchFiles(patch)[0]!.files[0]!;
    expect(fileDiff.isPartial).toBe(true);
    const lines = numbered(300).map((line) => (line === "line 4" ? "HIDDEN_NEEDLE" : line));
    const before = `${lines.join("\n")}\n`;
    const after = `${lines.map((line) => (line === "line 295" ? "line 295 changed" : line)).join("\n")}\n`;
    const load = async () => ({
      oldFile: { name: PATH, contents: before },
      newFile: { name: PATH, contents: after },
    });
    const source = { expandContext: { load, lineCount: 10 } };
    const view = render(reviewOf(fileDiff, { source }));
    await waitFor(() =>
      expect(renderedFile().querySelector("[data-expand-all-button]")).not.toBeNull(),
    );

    view.rerender(reviewOf(fileDiff, { source, findOpen: true }));
    fireEvent.change(screen.getByTestId("diff-review-find-input"), {
      target: { value: "HIDDEN_NEEDLE" },
    });
    await waitFor(() =>
      expect(screen.getByTestId("diff-review-find-status")).toHaveTextContent(/no results/i),
    );

    await act(async () => {
      fireEvent.click(renderedFile().querySelector("[data-expand-all-button]")!);
    });
    await waitFor(() =>
      expect(screen.getByTestId("diff-review-find-status")).toHaveTextContent(/1 of 1/i),
    );
  });

  // A whole-PR patch: old line 2 deleted, line 295 changed; everything else is left out.
  const patchWithOffset = [
    `diff --git a/${PATH} b/${PATH}`,
    "index 1111111..2222222 100644",
    `--- a/${PATH}`,
    `+++ b/${PATH}`,
    "@@ -1,3 +1,2 @@",
    " line 1",
    "-line 2",
    " line 3",
    "@@ -294,3 +293,3 @@",
    " line 294",
    "-line 295",
    "+line 295 changed",
    " line 296",
    "",
  ].join("\n");
  const fullText = (needle?: { line: number; text: string }) => {
    const lines = numbered(300).map((line, index) =>
      needle && index + 1 === needle.line ? needle.text : line,
    );
    const before = `${lines.join("\n")}\n`;
    const after = `${lines
      .filter((line) => line !== "line 2")
      .map((line) => (line === "line 295" ? "line 295 changed" : line))
      .join("\n")}\n`;
    return { oldFile: { name: PATH, contents: before }, newFile: { name: PATH, contents: after } };
  };

  it.each([
    ["the current side", { line: 50, side: "additions" as const }, 50],
    ["the old side", { line: 100, side: "deletions" as const }, 99],
  ])("loads a patch's full text to reveal a line it left out on %s", async (_, at, shownAs) => {
    const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
    let loads = 0;
    const load = async () => {
      loads += 1;
      return fullText();
    };
    const source = { expandContext: { load, lineCount: 10 } };
    const view = render(reviewOf(fileDiff, { source, revealPath: null }));
    await waitFor(() => expect(renderedFile().querySelector("[data-line]")).not.toBeNull());
    expect(loads).toBe(0);

    const target: ReviewRevealTarget = { path: PATH, ...at, align: "center" };
    view.rerender(reviewOf(fileDiff, { source, revealPath: null, revealTarget: target }));
    await waitFor(() => expect(sideRow(renderedFile(), at.line, at.side)).toBeDefined());
    expect(sideRow(renderedFile(), at.line, at.side)?.getAttribute("data-line")).toBe(
      String(shownAs),
    );
    expect(loads).toBe(1);
  });

  it("reveals a match a limited expansion loaded but didn't show", async () => {
    const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
    let loads = 0;
    const load = async () => {
      loads += 1;
      return fullText({ line: 150, text: "HIDDEN_NEEDLE" });
    };
    const source = { expandContext: { load, lineCount: 10 } };
    render(reviewOf(fileDiff, { source, revealPath: null, findOpen: true }));
    await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
    expect(sideRow(renderedFile(), 8, "additions")).toBeUndefined();
    fireEvent.change(screen.getByTestId("diff-review-find-input"), {
      target: { value: "HIDDEN_NEEDLE" },
    });
    await waitFor(() =>
      expect(screen.getByTestId("diff-review-find-status")).toHaveTextContent(/no results/i),
    );

    // Ten more lines after the first hunk, current lines 3–12. Its full text has
    // the match on line 149, which the expansion itself leaves hidden.
    await act(async () => {
      fireEvent.click(renderedFile().querySelectorAll("[data-expand-up]")[0]!);
    });
    await waitFor(() =>
      expect(screen.getByTestId("diff-review-find-status")).toHaveTextContent(/1 of 1/i),
    );
    await waitFor(() => expect(sideRow(renderedFile(), 149, "additions")).toBeDefined());
    expect(sideRow(renderedFile(), 149, "additions")?.textContent).toBe("HIDDEN_NEEDLE");
    expect(loads).toBe(1);
  });

  it("keeps the reader's limited expansion once the full text replaces the patch", async () => {
    const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
    const source = { expandContext: { load: async () => fullText(), lineCount: 10 } };
    render(reviewOf(fileDiff, { source, revealPath: null }));
    await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
    await act(async () => {
      fireEvent.click(renderedFile().querySelectorAll("[data-expand-up]")[0]!);
    });
    await waitFor(() => expect(sideRow(renderedFile(), 8, "additions")).toBeDefined());
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 200);
      });
    });
    expect(sideRow(renderedFile(), 8, "additions")).toBeDefined();
  });

  describe("expanding unchanged lines while their file loads", () => {
    const hosts = () => [...document.querySelectorAll("diffs-container")];
    const firstExpandUp = (root = renderedFile()) =>
      root.querySelectorAll<HTMLElement>("[data-expand-up]")[0]!;
    type Loaded = ReturnType<typeof fullText>;
    /** A load that finishes only when the test says so. */
    function deferredLoad() {
      const finishers: ((files: Loaded) => void)[] = [];
      const load = vi.fn(
        () =>
          new Promise<Loaded>((resolve) => {
            finishers.push(resolve);
          }),
      );
      return { load, finish: (files = fullText()) => finishers.shift()?.(files) };
    }
    /** Clicks the library receives inside the file; the host's guard stops the rest. */
    const libraryClicks = () => {
      const received = vi.fn();
      renderedFile().addEventListener("click", received);
      return received;
    };
    const expandOnce = async (root?: ShadowRoot) => {
      await act(async () => {
        fireEvent.click(firstExpandUp(root));
      });
    };

    it("shows it's loading at once, ignores repeat clicks, then shows the lines", async () => {
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const { load, finish } = deferredLoad();
      render(reviewOf(fileDiff, { source: { expandContext: { load, lineCount: 10 } } }));
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());

      const received = libraryClicks();
      await expandOnce();
      expect(load).toHaveBeenCalledTimes(1);
      expect(await screen.findByRole("status", { name: "" })).toHaveTextContent(
        "Loading unchanged lines…",
      );
      await waitFor(() => expect(hosts()[0]!.hasAttribute("data-context-loading")).toBe(true));
      // A second click while the lines load would ask for ten more; the library never gets it.
      await expandOnce();
      expect(received).toHaveBeenCalledTimes(1);

      await act(async () => finish());
      await waitFor(() => expect(sideRow(renderedFile(), 8, "additions")).toBeDefined());
      expect(sideRow(renderedFile(), 15, "additions")).toBeUndefined();
      expect(load).toHaveBeenCalledTimes(1);
      expect(hosts()[0]!.hasAttribute("data-context-loading")).toBe(false);
      expect(screen.queryByText("Loading unchanged lines…")).toBeNull();
    });

    it("ignores the unmodified-lines label while the lines load, after an arrow click too", async () => {
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const { load, finish } = deferredLoad();
      render(reviewOf(fileDiff, { source: { expandContext: { load, lineCount: 10 } } }));
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
      const received = libraryClicks();
      await expandOnce();
      await waitFor(() => expect(hosts()[0]!.hasAttribute("data-context-loading")).toBe(true));
      // The label expands too, from itself or its text.
      const label = renderedFile().querySelectorAll<HTMLElement>("[data-unmodified-lines]")[0]!;
      await act(async () => {
        fireEvent.click(label);
        fireEvent.click(label.firstChild ?? label);
      });
      expect(received).toHaveBeenCalledTimes(1);
      await act(async () => finish());
      await waitFor(() => expect(hosts()[0]!.hasAttribute("data-context-loading")).toBe(false));
      expect(load).toHaveBeenCalledTimes(1);
      // Once the lines are in, the label expands again.
      const after = renderedFile().querySelector<HTMLElement>("[data-unmodified-lines]");
      if (after) {
        await act(async () => {
          fireEvent.click(after);
        });
        expect(received).toHaveBeenCalledTimes(2);
      }
    });

    it("says the lines didn't load, and the header's Retry loads the lines asked for", async () => {
      vi.spyOn(console, "error").mockImplementation(() => undefined);
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const load = vi
        .fn<() => Promise<Loaded>>()
        .mockRejectedValueOnce(new Error("The host didn’t answer"))
        .mockResolvedValue(fullText());
      render(reviewOf(fileDiff, { source: { expandContext: { load, lineCount: 10 } } }));
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());

      await expandOnce();
      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load unchanged lines");
      await waitFor(() => expect(hosts()[0]!.hasAttribute("data-context-failed")).toBe(true));

      const retry = screen.getByRole("button", {
        name: `Retry loading unchanged lines of ${PATH}`,
      });
      retry.focus();
      expect(retry).toHaveFocus();
      await act(async () => {
        fireEvent.click(retry);
      });
      expect(load).toHaveBeenCalledTimes(2);
      // The expansion asked for before the failure shows, and no more.
      await waitFor(() => expect(sideRow(renderedFile(), 8, "additions")).toBeDefined());
      expect(sideRow(renderedFile(), 15, "additions")).toBeUndefined();
      expect(hosts()[0]!.hasAttribute("data-context-failed")).toBe(false);
      expect(screen.queryByRole("alert")).toBeNull();
      vi.restoreAllMocks();
    });

    it("drops what a replaced loader finishes, so the diff doesn't take it", async () => {
      vi.spyOn(console, "error").mockImplementation(() => undefined);
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const first = deferredLoad();
      const view = render(
        reviewOf(fileDiff, { source: { expandContext: { load: first.load, lineCount: 10 } } }),
      );
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
      await expandOnce();
      // Another head arrives with the same patch: a new loader for the same diff.
      const second = deferredLoad();
      view.rerender(
        reviewOf(fileDiff, { source: { expandContext: { load: second.load, lineCount: 10 } } }),
      );
      await act(async () => first.finish(fullText({ line: 8, text: "STALE" })));
      await act(async () => {
        await new Promise((resolve) => {
          setTimeout(resolve, 50);
        });
      });
      expect(sideRow(renderedFile(), 8, "additions")).toBeUndefined();
      expect(fileDiff.isPartial).toBe(true);
      expect(screen.queryByRole("alert")).toBeNull();
      expect(hosts()[0]!.hasAttribute("data-context-failed")).toBe(false);
      vi.restoreAllMocks();
    });

    it("leaves nothing behind when the review closes while lines load", async () => {
      vi.spyOn(console, "error").mockImplementation(() => undefined);
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const { load, finish } = deferredLoad();
      const view = render(
        reviewOf(fileDiff, { source: { expandContext: { load, lineCount: 10 } } }),
      );
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
      await expandOnce();
      view.unmount();
      await act(async () => finish());
      expect(fileDiff.isPartial).toBe(true);
      // Opening it again starts from the patch, ready to load again.
      render(reviewOf(fileDiff, { source: { expandContext: { load, lineCount: 10 } } }));
      await waitFor(() => expect(renderedFile().querySelector("[data-expand-up]")).not.toBeNull());
      expect(screen.queryByText("Loading unchanged lines…")).toBeNull();
      vi.restoreAllMocks();
    });
  });

  describe("loading a patch's full text for a reveal", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });
    const lineScrolls = (spy: { mock: { calls: [CodeViewScrollTarget][] } }) =>
      spy.mock.calls.map(([target]) => target).filter((target) => target.type === "line");

    it("asks once per reveal when the load fails, and a later reveal tries again", async () => {
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      let available = false;
      let loads = 0;
      const load = async () => {
        loads += 1;
        if (!available) throw new Error("Forbidden");
        return fullText();
      };
      const source = { expandContext: { load, lineCount: 10 } };
      const scroll = vi.spyOn(CodeView.prototype, "scrollTo");
      const view = render(reviewOf(fileDiff, { source, revealPath: null }));
      await waitFor(() => expect(renderedFile().querySelector("[data-line]")).not.toBeNull());

      const first: ReviewRevealTarget = {
        path: PATH,
        line: 50,
        side: "additions",
        align: "center",
      };
      view.rerender(reviewOf(fileDiff, { source, revealPath: null, revealTarget: first }));
      // The reveal gives up on the missing text and scrolls as near as it can.
      await waitFor(() => expect(lineScrolls(scroll)).toHaveLength(1));
      await act(async () => {
        await new Promise((resolve) => {
          setTimeout(resolve, 300);
        });
      });
      expect(loads).toBe(1);
      expect(sideRow(renderedFile(), 50, "additions")).toBeUndefined();

      available = true;
      const again: ReviewRevealTarget = { ...first };
      view.rerender(reviewOf(fileDiff, { source, revealPath: null, revealTarget: again }));
      await waitFor(() => expect(sideRow(renderedFile(), 50, "additions")).toBeDefined());
      expect(loads).toBe(2);
    });

    it("loads a refreshed patch that replaces one still loading for a reveal", async () => {
      const first = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      const loads: { fileDiff: FileDiffMetadata; resolve: () => void }[] = [];
      const load = (fileDiff: FileDiffMetadata) =>
        new Promise<ReturnType<typeof fullText>>((resolve) => {
          loads.push({ fileDiff, resolve: () => resolve(fullText()) });
        });
      const source = { expandContext: { load, lineCount: 10 } };
      const target: ReviewRevealTarget = {
        path: PATH,
        line: 50,
        side: "additions",
        align: "center",
      };
      const view = render(reviewOf(first, { source, revealPath: null }));
      await waitFor(() => expect(renderedFile().querySelector("[data-line]")).not.toBeNull());
      view.rerender(reviewOf(first, { source, revealPath: null, revealTarget: target }));
      await waitFor(() => expect(loads).toHaveLength(1));

      // A refresh brings the same patch as new metadata while the first load is pending.
      const refreshed = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      view.rerender(reviewOf(refreshed, { source, revealPath: null, revealTarget: target }));
      await waitFor(() => expect(loads).toHaveLength(2));
      expect(loads[1]!.fileDiff).toBe(refreshed);

      // The first result arrives late and belongs to metadata the review no longer shows.
      await act(async () => loads[0]!.resolve());
      expect(sideRow(renderedFile(), 50, "additions")).toBeUndefined();
      await act(async () => loads[1]!.resolve());
      await waitFor(() => expect(sideRow(renderedFile(), 50, "additions")).toBeDefined());
      expect(loads).toHaveLength(2);
    });

    it.each([
      ["a changed line on the old side", { line: 295, side: "deletions" as const }],
      ["an unchanged line the patch has", { line: 294, side: "additions" as const }],
    ])("scrolls to %s right away without loading", async (_, at) => {
      const fileDiff = parsePatchFiles(patchWithOffset)[0]!.files[0]!;
      let loads = 0;
      const load = () => {
        loads += 1;
        // Never settles, like a request that is still in flight.
        return new Promise<never>(() => {});
      };
      const source = { expandContext: { load, lineCount: 10 } };
      const scroll = vi.spyOn(CodeView.prototype, "scrollTo");
      const view = render(reviewOf(fileDiff, { source, revealPath: null }));
      await waitFor(() => expect(renderedFile().querySelector("[data-line]")).not.toBeNull());

      const target: ReviewRevealTarget = { path: PATH, ...at, align: "center" };
      view.rerender(reviewOf(fileDiff, { source, revealPath: null, revealTarget: target }));
      await waitFor(
        () =>
          expect(lineScrolls(scroll)).toEqual([
            expect.objectContaining({ lineNumber: at.line, side: at.side }),
          ]),
        { timeout: 500 },
      );
      expect(loads).toBe(0);
    });
  });
});
