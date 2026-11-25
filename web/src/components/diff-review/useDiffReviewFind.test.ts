import { act, render, renderHook, waitFor } from "@testing-library/react";
import { createElement, Suspense, useLayoutEffect } from "react";
import { flushSync } from "react-dom";
import { describe, expect, it, vi } from "vitest";
import type { ReviewSearchLine, ReviewSearchMatch } from "./findMatches";
import { type DiffReviewFind, useDiffReviewFind } from "./useDiffReviewFind";

const lines: ReviewSearchLine[] = [
  { id: "diff:a.ts", text: "needle one", lineNumber: 1, side: "additions" },
  { id: "diff:b.ts", text: "needle two", lineNumber: 4, side: "additions" },
  { id: "diff:c.ts", text: "needle three", lineNumber: 9, side: "deletions" },
];

function setup(startingIndex = 0, open = true) {
  const reveal = vi.fn();
  const clear = vi.fn();
  const hook = renderHook(
    ({ isOpen }) =>
      useDiffReviewFind(isOpen, lines, { reveal, clear, startingIndex: () => startingIndex }),
    { initialProps: { isOpen: open } },
  );
  return { ...hook, reveal, clear };
}

describe("useDiffReviewFind", () => {
  it("starts at the match the caller picks and steps around the ends", async () => {
    const { result, reveal } = setup(1);
    act(() => result.current.setQuery("needle"));
    await waitFor(() => expect(result.current.status).toBe("2 of 3"));
    expect(reveal).toHaveBeenLastCalledWith(expect.objectContaining({ id: "diff:b.ts" }));
    act(() => result.current.next());
    expect(result.current.status).toBe("3 of 3");
    act(() => result.current.next());
    expect(result.current.status).toBe("1 of 3");
    act(() => result.current.previous());
    expect(result.current.status).toBe("3 of 3");
    expect(reveal).toHaveBeenLastCalledWith(expect.objectContaining({ id: "diff:c.ts" }));
  });

  it("answers while the review around it keeps rendering, as reads come and go", async () => {
    const { result, rerender } = setup();
    act(() => result.current.setQuery("needle"));
    // The panel renders again for each read that starts or finishes; the lines stay the same.
    for (let pass = 0; pass < 5; pass++) rerender({ isOpen: true });
    await waitFor(() => expect(result.current.status).toBe("1 of 3"));
    expect(result.current.searching).toBe(false);
  });

  it("clamps an out-of-range starting match", async () => {
    const { result } = setup(99);
    act(() => result.current.setQuery("needle"));
    await waitFor(() => expect(result.current.status).toBe("3 of 3"));
  });

  it("clears the painted match when nothing matches or find closes", async () => {
    const { result, clear, rerender } = setup();
    act(() => result.current.setQuery("haystack"));
    await waitFor(() => expect(result.current.status).toBe("No results"));
    expect(result.current.hasMatches).toBe(false);
    expect(clear).toHaveBeenCalled();
    clear.mockClear();
    rerender({ isOpen: false });
    expect(clear).toHaveBeenCalled();
  });

  it("applies match options", async () => {
    const { result } = setup();
    act(() => result.current.setQuery("NEEDLE"));
    await waitFor(() => expect(result.current.status).toBe("1 of 3"));
    act(() => result.current.setOptions({ caseSensitive: true, wholeWord: false, regexp: false }));
    await waitFor(() => expect(result.current.status).toBe("No results"));
  });

  it("does nothing while closed", () => {
    const { result, reveal } = setup(0, false);
    act(() => result.current.setQuery("needle"));
    expect(reveal).not.toHaveBeenCalled();
  });
});

describe("useDiffReviewFind's hidden matches", () => {
  const shown: ReviewSearchLine[] = [{ id: "shown", text: "alpha beta", lineNumber: 1 }];
  const hidden: ReviewSearchLine[] = [
    { id: "hidden", text: "beta beta", lineNumber: 1 },
    { id: "hidden", text: "gamma", lineNumber: 2 },
  ];
  const callbacks = { reveal: vi.fn(), clear: vi.fn(), startingIndex: () => 0 };
  const hook = (open: boolean, hiddenLines: ReviewSearchLine[] = hidden) =>
    renderHook(
      ({ isOpen }: { isOpen: boolean }) => useDiffReviewFind(isOpen, shown, callbacks, hiddenLines),
      { initialProps: { isOpen: open } },
    );

  it("counts the query's matches in the files the review leaves out, following the query", async () => {
    const { result } = hook(true);
    act(() => result.current.setQuery("beta"));
    await waitFor(() => expect(result.current.hidden).toEqual({ count: 2, truncated: false }));
    expect(result.current.hasMatches).toBe(true);
    act(() => result.current.setQuery("gamma"));
    await waitFor(() => expect(result.current.hidden.count).toBe(1));
    // With nothing shown, the hidden ones are all there is.
    expect(result.current.hasMatches).toBe(false);
  });

  it("follows the options, and has none for an invalid expression", async () => {
    const { result } = hook(true);
    act(() => result.current.setQuery("BETA"));
    await waitFor(() => expect(result.current.hidden.count).toBe(2));
    act(() => result.current.setOptions({ ...result.current.options, caseSensitive: true }));
    await waitFor(() => expect(result.current.hidden.count).toBe(0));
    act(() => {
      result.current.setOptions({ ...result.current.options, caseSensitive: false, regexp: true });
      result.current.setQuery("(");
    });
    // Shown as invalid in the bar, with no hidden note to go with it.
    await waitFor(() => expect(result.current.status).toMatch(/invalid/i));
    expect(result.current.hidden.count).toBe(0);
  });

  it("says when the count reached the match limit", async () => {
    const many: ReviewSearchLine[] = Array.from({ length: 20_001 }, (_, index) => ({
      id: "hidden",
      text: "x",
      lineNumber: index + 1,
    }));
    const { result } = hook(true, many);
    act(() => result.current.setQuery("x"));
    await waitFor(() => expect(result.current.hidden).toEqual({ count: 20_000, truncated: true }));
  });

  it("searches no hidden lines while find is closed", async () => {
    const { result, rerender } = hook(false);
    act(() => result.current.setQuery("beta"));
    await waitFor(() => expect(result.current.query).toBe("beta"));
    expect(result.current.hidden.count).toBe(0);
    rerender({ isOpen: true });
    await waitFor(() => expect(result.current.hidden.count).toBe(2));
  });
});

describe("useDiffReviewFind while a newer query's result is deferred", () => {
  // A review whose newer query's deferred render can be held, as a slow search would, until released.
  function heldFindReview() {
    // The review's request token, as DiffReview keeps it: a paint queued under an older
    // request does nothing once a reveal or clear has started a newer one.
    let request = 0;
    const queued: (() => void)[] = [];
    const painted: string[] = [];
    const reveal = vi.fn((match: ReviewSearchMatch) => {
      const mine = ++request;
      queued.push(() => {
        if (mine === request) painted.push(`${match.id}:${match.lineNumber}`);
      });
    });
    const clear = vi.fn(() => {
      request += 1;
    });
    const findLines: ReviewSearchLine[] = [
      { id: "a", text: "needle", lineNumber: 1 },
      { id: "b", text: "missing piece", lineNumber: 2 },
    ];
    let releaseGate: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      releaseGate = resolve;
    });
    let holding = true;
    // What the committed UI shows: a held render never reaches it.
    let find: DiffReviewFind | undefined;
    function Gate({ rendered }: { rendered: DiffReviewFind }) {
      if (holding && rendered.query === "missing" && !rendered.searching) throw gate;
      return null;
    }
    function Harness() {
      const rendered = useDiffReviewFind(true, findLines, {
        reveal,
        clear,
        startingIndex: () => 0,
      });
      useLayoutEffect(() => {
        find = rendered;
      });
      return createElement(Gate, { rendered });
    }
    render(createElement(Suspense, { fallback: null }, createElement(Harness)));
    return {
      find: () => find!,
      reveal,
      clear,
      painted,
      /** Runs every queued paint, as the next animation frame would. */
      flushPaints: () => {
        for (const paint of queued.splice(0)) paint();
      },
      release: async () => {
        holding = false;
        await act(async () => {
          releaseGate();
          await gate;
        });
      },
    };
  }

  it("withdraws the old match when the query is accepted, before its deferred result commits", async () => {
    const review = heldFindReview();
    await act(async () => review.find().setQuery("needle"));
    await waitFor(() => expect(review.reveal).toHaveBeenCalledTimes(1));
    const clearsBefore = review.clear.mock.calls.length;

    // Accepting the newer query withdraws the old match synchronously, while its result waits.
    act(() => flushSync(() => review.find().setQuery("missing")));
    expect(review.find().query).toBe("missing");
    expect(review.find().searching).toBe(true);
    expect(review.clear.mock.calls.length).toBe(clearsBefore + 1);
    // The old paint, run now, paints nothing; Next doesn't move through the old results.
    review.flushPaints();
    expect(review.painted).toEqual([]);
    act(() => review.find().next());
    expect(review.reveal).toHaveBeenCalledTimes(1);

    // Released, only the newer query's result reveals and paints.
    await review.release();
    await waitFor(() => expect(review.find().searching).toBe(false));
    await waitFor(() => expect(review.reveal).toHaveBeenCalledTimes(2));
    review.flushPaints();
    expect(review.painted).toEqual(["b:2"]);
  });

  it("shows the first query's match again when it returns while the newer one is held", async () => {
    const review = heldFindReview();
    await act(async () => review.find().setQuery("needle"));
    await waitFor(() => expect(review.reveal).toHaveBeenCalledTimes(1));
    review.flushPaints();
    expect(review.painted).toEqual(["a:1"]);

    act(() => flushSync(() => review.find().setQuery("missing")));
    expect(review.find().searching).toBe(true);
    // Back to the first query before the newer one's result arrives: its match is revealed
    // again, though its result never changed.
    act(() => flushSync(() => review.find().setQuery("needle")));
    expect(review.find().searching).toBe(false);
    expect(review.reveal).toHaveBeenCalledTimes(2);
    expect(review.reveal).toHaveBeenLastCalledWith(expect.objectContaining({ id: "a" }));
    review.flushPaints();
    expect(review.painted).toEqual(["a:1", "a:1"]);
    expect(review.find().status).toBe("1 of 1");
    act(() => review.find().next());
    expect(review.reveal).toHaveBeenCalledTimes(3);

    // The abandoned query's render, released late, reveals and paints nothing.
    await review.release();
    review.flushPaints();
    expect(review.reveal).toHaveBeenCalledTimes(3);
    expect(review.painted).toEqual(["a:1", "a:1", "a:1"]);
    expect(review.find().query).toBe("needle");
  });

  it("shows the match again when a newer query and the first one come in one update", async () => {
    const review = heldFindReview();
    await act(async () => review.find().setQuery("needle"));
    await waitFor(() => expect(review.reveal).toHaveBeenCalledTimes(1));
    review.flushPaints();
    const clearsBefore = review.clear.mock.calls.length;

    act(() => {
      review.find().setQuery("missing");
      review.find().setQuery("needle");
    });
    // Both acceptances withdrew the shown match; the first query's match then came back.
    expect(review.clear.mock.calls.length).toBe(clearsBefore + 2);
    expect(review.reveal).toHaveBeenCalledTimes(2);
    review.flushPaints();
    expect(review.painted).toEqual(["a:1", "a:1"]);
    expect(review.find().searching).toBe(false);
  });

  it("withdraws the old match when the options change", async () => {
    const reveal = vi.fn();
    const clear = vi.fn();
    const { result } = renderHook(() =>
      useDiffReviewFind(true, [{ id: "a", text: "Needle", lineNumber: 1 }], {
        reveal,
        clear,
        startingIndex: () => 0,
      }),
    );
    act(() => result.current.setQuery("needle"));
    await waitFor(() => expect(reveal).toHaveBeenCalledTimes(1));
    const clears = clear.mock.calls.length;
    act(() => result.current.setOptions({ ...result.current.options }));
    // The same options again withdraw nothing.
    expect(clear.mock.calls.length).toBe(clears);
    // Whole word still matches, so only accepting the change itself can have withdrawn the match;
    // the match is then revealed again under the new options.
    const reveals = reveal.mock.calls.length;
    act(() => result.current.setOptions({ ...result.current.options, wholeWord: true }));
    expect(clear.mock.calls.length).toBe(clears + 1);
    await waitFor(() => expect(reveal.mock.calls.length).toBeGreaterThan(reveals));
  });
});
