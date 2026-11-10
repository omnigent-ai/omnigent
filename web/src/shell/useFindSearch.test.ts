import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as PatternSearchModule from "./pullRequestPatternSearch";
import {
  FAILED,
  searchPatternInWorker,
  TOO_SLOW,
  type FindOutcome,
} from "./pullRequestPatternSearch";
import type { PatchMatchTarget } from "./pullRequestReview";
import { useFindSearch } from "./useFindSearch";

// The worker is replaced by a runner the test settles by hand, so each run's timing is explicit.
vi.mock("./pullRequestPatternSearch", async (importOriginal) => ({
  ...(await importOriginal<typeof PatternSearchModule>()),
  searchPatternInWorker: vi.fn(),
}));

interface PendingRun {
  corpus: readonly PatchMatchTarget[];
  query: string;
  signal: AbortSignal | undefined;
  settle: (outcome: FindOutcome | null) => void;
}

let runs: PendingRun[];

const CORPUS_A: PatchMatchTarget[] = [
  { path: "a.ts", kind: "line", side: "additions", line: 1, text: "needle" },
];
const CORPUS_B: PatchMatchTarget[] = [
  { path: "b.ts", kind: "line", side: "additions", line: 4, text: "needle again" },
];
const REGEX = { matchCase: false, regex: true };
const FOUND: FindOutcome = {
  matches: [{ target: CORPUS_A[0], start: 0, end: 6 }],
  truncated: false,
  invalid: false,
  tooSlow: false,
  failed: false,
};
const NOTHING_YET = {
  matches: [],
  truncated: false,
  invalid: false,
  tooSlow: false,
  failed: false,
  searching: true,
};

beforeEach(() => {
  runs = [];
  vi.mocked(searchPatternInWorker).mockImplementation(
    (corpus, query, _options, signal) =>
      new Promise((settle) => {
        runs.push({ corpus, query, signal, settle });
      }),
  );
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useFindSearch", () => {
  it("matches a plain substring synchronously without a worker", () => {
    const { result } = renderHook(() =>
      useFindSearch(CORPUS_A, "needle", { matchCase: false, regex: false }),
    );

    expect(result.current).toEqual({ ...FOUND, searching: false });
    expect(searchPatternInWorker).not.toHaveBeenCalled();
  });

  it("reports searching until the worker's result arrives", async () => {
    const { result } = renderHook(() => useFindSearch(CORPUS_A, "needle", REGEX));
    expect(result.current).toEqual(NOTHING_YET);

    await act(async () => runs[0].settle(FOUND));
    expect(result.current).toEqual({ ...FOUND, searching: false });
  });

  it("skips the worker for an empty corpus", () => {
    const { result } = renderHook(() => useFindSearch([], "needle", REGEX));

    expect(result.current.searching).toBe(false);
    expect(searchPatternInWorker).not.toHaveBeenCalled();
  });

  it.each(["", "   "])("skips the worker for the blank query %j", (query) => {
    const { result } = renderHook(() => useFindSearch(CORPUS_A, query, REGEX));

    expect(result.current.searching).toBe(false);
    expect(searchPatternInWorker).not.toHaveBeenCalled();
  });

  it.each([
    ["timed out", TOO_SLOW],
    ["failed", FAILED],
  ])(
    "shows nothing while a refreshed corpus is searched after a result that %s",
    async (_label, earlier) => {
      const { result, rerender } = renderHook(
        ({ corpus }) => useFindSearch(corpus, "needle", REGEX),
        { initialProps: { corpus: CORPUS_A } },
      );
      await act(async () => runs[0].settle(earlier));
      expect(result.current).toEqual({ ...earlier, searching: false });

      rerender({ corpus: CORPUS_B });

      expect(result.current).toEqual(NOTHING_YET);
      expect(runs).toHaveLength(2);
    },
  );

  it("shows no earlier failure while the same query is searched again", async () => {
    const { result, rerender } = renderHook(({ query }) => useFindSearch(CORPUS_A, query, REGEX), {
      initialProps: { query: "needle" },
    });
    await act(async () => runs[0].settle(FAILED));

    rerender({ query: "other" });
    rerender({ query: "needle" });

    expect(result.current).toEqual(NOTHING_YET);
    expect(runs).toHaveLength(3);
    await act(async () => runs[2].settle(FOUND));
    expect(result.current).toEqual({ ...FOUND, searching: false });
  });

  it("drops the previous matches when the query changes", async () => {
    const { result, rerender } = renderHook(({ query }) => useFindSearch(CORPUS_A, query, REGEX), {
      initialProps: { query: "needle" },
    });
    await act(async () => runs[0].settle(FOUND));

    rerender({ query: "other" });

    expect(result.current).toEqual(NOTHING_YET);
  });

  it("aborts a superseded search and ignores its late result", async () => {
    const { result, rerender } = renderHook(({ query }) => useFindSearch(CORPUS_A, query, REGEX), {
      initialProps: { query: "needle" },
    });
    rerender({ query: "other" });
    expect(runs[0].signal?.aborted).toBe(true);

    await act(async () => runs[0].settle(FOUND));

    expect(result.current).toEqual(NOTHING_YET);
  });

  it("ignores a superseded corpus's late result under the same query", async () => {
    const { result, rerender } = renderHook(
      ({ corpus }) => useFindSearch(corpus, "needle", REGEX),
      { initialProps: { corpus: CORPUS_A } },
    );
    rerender({ corpus: CORPUS_B });
    expect(runs[0].signal?.aborted).toBe(true);

    await act(async () => runs[0].settle(FOUND));
    expect(result.current).toEqual(NOTHING_YET);

    await act(async () => runs[1].settle({ ...FOUND, matches: [] }));
    expect(result.current).toEqual({ ...FOUND, matches: [], searching: false });
  });

  it("settles as failed when the worker promise rejects", async () => {
    vi.mocked(searchPatternInWorker).mockImplementationOnce(() =>
      Promise.reject(new Error("worker unavailable")),
    );
    const { result } = renderHook(() => useFindSearch(CORPUS_A, "needle", REGEX));

    await waitFor(() => expect(result.current.searching).toBe(false));
    expect(result.current).toEqual({ ...FAILED, searching: false });
  });

  it("aborts the running search on unmount", () => {
    const { unmount } = renderHook(() => useFindSearch(CORPUS_A, "needle", REGEX));

    unmount();

    expect(runs[0].signal?.aborted).toBe(true);
  });

  it("searches again when the match case option changes with the same corpus and query", async () => {
    const { result, rerender } = renderHook(
      ({ matchCase }) => useFindSearch(CORPUS_A, "needle", { matchCase, regex: true }),
      { initialProps: { matchCase: false } },
    );
    await act(async () => runs[0].settle(FOUND));

    rerender({ matchCase: true });

    expect(runs).toHaveLength(2);
    expect(runs[0].signal?.aborted).toBe(true);
    expect(result.current).toEqual(NOTHING_YET);
  });

  it("returns plain matches at once when regex mode is left with a search outstanding", async () => {
    const { result, rerender } = renderHook(
      ({ regex }) => useFindSearch(CORPUS_A, "needle", { matchCase: false, regex }),
      { initialProps: { regex: true } },
    );

    rerender({ regex: false });

    expect(runs[0].signal?.aborted).toBe(true);
    expect(result.current).toEqual({ ...FOUND, searching: false });
    await act(async () => runs[0].settle(FAILED));
    expect(result.current).toEqual({ ...FOUND, searching: false });
  });

  it("searches again when regex mode is re-entered with the same corpus and query", async () => {
    const { result, rerender } = renderHook(
      ({ regex }) => useFindSearch(CORPUS_A, "needle", { matchCase: false, regex }),
      { initialProps: { regex: true } },
    );
    await act(async () => runs[0].settle(FOUND));

    rerender({ regex: false });
    expect(result.current.searching).toBe(false);
    rerender({ regex: true });

    expect(runs).toHaveLength(2);
    expect(result.current).toEqual(NOTHING_YET);
  });
});
