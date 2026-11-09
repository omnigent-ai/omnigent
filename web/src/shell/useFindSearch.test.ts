import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { searchPatternInWorker, type FindOutcome } from "./pullRequestPatternSearch";
import type { PatchMatchTarget } from "./pullRequestReview";
import type * as PatternSearchModule from "./pullRequestPatternSearch";
import { useFindSearch } from "./useFindSearch";

vi.mock("./pullRequestPatternSearch", async (importOriginal) => ({
  ...(await importOriginal<typeof PatternSearchModule>()),
  searchPatternInWorker: vi.fn(),
}));

const search = vi.mocked(searchPatternInWorker);
const OPTIONS = { matchCase: false, regex: true };

const LINE: PatchMatchTarget = {
  path: "a.ts",
  kind: "line",
  side: "additions",
  line: 2,
  text: "needle here",
};
const CORPUS = [LINE];
const OTHER_CORPUS = [{ ...LINE, line: 9 }];

function outcome(matches: FindOutcome["matches"]): FindOutcome {
  return { matches, truncated: false, invalid: false, tooSlow: false, failed: false };
}
const FOUND = outcome([{ target: LINE, start: 0, end: 6 }]);
const EMPTY = outcome([]);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  search.mockReset();
});

describe("useFindSearch", () => {
  it("matches a plain substring synchronously without a worker", () => {
    const { result } = renderHook(() =>
      useFindSearch(CORPUS, "needle", { matchCase: false, regex: false }),
    );

    expect(result.current).toMatchObject({ searching: false, matches: [{ target: LINE }] });
    expect(search).not.toHaveBeenCalled();
  });

  it("skips the worker for an empty corpus", () => {
    const { result } = renderHook(() => useFindSearch([], "needle", OPTIONS));

    expect(result.current).toMatchObject({ searching: false, matches: [] });
    expect(search).not.toHaveBeenCalled();
  });

  it("skips the worker for a blank query against a nonempty corpus", () => {
    const { result } = renderHook(() => useFindSearch(CORPUS, "  ", OPTIONS));

    expect(result.current).toMatchObject({ searching: false, matches: [] });
    expect(search).not.toHaveBeenCalled();
  });

  it("reports searching until the worker's result arrives", async () => {
    const pending = deferred<FindOutcome | null>();
    search.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useFindSearch(CORPUS, "needle", OPTIONS));

    expect(result.current.searching).toBe(true);
    await act(async () => pending.resolve(FOUND));

    expect(result.current).toMatchObject({ searching: false, matches: [{ target: LINE }] });
  });

  it("keeps the last matches while a refreshed corpus is searched under the same query", async () => {
    const refreshed = deferred<FindOutcome | null>();
    search.mockResolvedValueOnce(FOUND).mockReturnValueOnce(refreshed.promise);
    const { result, rerender } = renderHook(
      ({ corpus }) => useFindSearch(corpus, "needle", OPTIONS),
      { initialProps: { corpus: CORPUS } },
    );
    await waitFor(() => expect(result.current.searching).toBe(false));

    rerender({ corpus: OTHER_CORPUS });
    expect(result.current).toMatchObject({ searching: true, matches: [{ target: LINE }] });

    await act(async () => refreshed.resolve(EMPTY));
    expect(result.current).toMatchObject({ searching: false, matches: [] });
  });

  it("drops the previous matches when the query changes", async () => {
    const replaced = deferred<FindOutcome | null>();
    search.mockResolvedValueOnce(FOUND).mockReturnValueOnce(replaced.promise);
    const { result, rerender } = renderHook(({ query }) => useFindSearch(CORPUS, query, OPTIONS), {
      initialProps: { query: "needle" },
    });
    await waitFor(() => expect(result.current.searching).toBe(false));

    rerender({ query: "other" });

    expect(result.current).toMatchObject({ searching: true, matches: [] });
  });

  it("aborts a superseded search and ignores its late result", async () => {
    const first = deferred<FindOutcome | null>();
    const second = deferred<FindOutcome | null>();
    search.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(({ query }) => useFindSearch(CORPUS, query, OPTIONS), {
      initialProps: { query: "needle" },
    });
    const firstSignal = search.mock.calls[0][3];

    rerender({ query: "other" });
    expect(firstSignal?.aborted).toBe(true);

    await act(async () => first.resolve(FOUND));
    expect(result.current).toMatchObject({ searching: true, matches: [] });

    await act(async () => second.resolve(EMPTY));
    expect(result.current).toMatchObject({ searching: false, matches: [] });
  });

  it("aborts the running search on unmount", () => {
    search.mockReturnValueOnce(deferred<FindOutcome | null>().promise);
    const { unmount } = renderHook(() => useFindSearch(CORPUS, "needle", OPTIONS));
    const signal = search.mock.calls[0][3];

    unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("searches again when regex mode is re-entered with the same corpus and query", async () => {
    const rerun = deferred<FindOutcome | null>();
    search.mockResolvedValueOnce(FOUND).mockReturnValueOnce(rerun.promise);
    const { result, rerender } = renderHook(
      ({ regex }) => useFindSearch(CORPUS, "needle", { matchCase: false, regex }),
      { initialProps: { regex: true } },
    );
    await waitFor(() => expect(result.current.searching).toBe(false));

    rerender({ regex: false });
    rerender({ regex: true });

    expect(result.current).toMatchObject({ searching: true, matches: [] });
    await act(async () => rerun.resolve(EMPTY));
    expect(result.current).toMatchObject({ searching: false, matches: [] });
  });

  it("settles as failed when the worker promise rejects", async () => {
    search.mockRejectedValueOnce(new Error("DataCloneError"));
    const { result } = renderHook(() => useFindSearch(CORPUS, "needle", OPTIONS));

    await waitFor(() => expect(result.current.searching).toBe(false));
    expect(result.current).toMatchObject({ failed: true, matches: [] });
  });

  it("ignores a superseded corpus's late result under the same query", async () => {
    const stale = deferred<FindOutcome | null>();
    const fresh = deferred<FindOutcome | null>();
    search.mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise);
    const { result, rerender } = renderHook(
      ({ corpus }) => useFindSearch(corpus, "needle", OPTIONS),
      { initialProps: { corpus: CORPUS } },
    );

    rerender({ corpus: OTHER_CORPUS });
    // The worker ignores the abort and still resolves a match for the old corpus.
    await act(async () => stale.resolve(FOUND));
    expect(result.current).toMatchObject({ searching: true, matches: [] });

    await act(async () => fresh.resolve(EMPTY));
    expect(result.current).toMatchObject({ searching: false, matches: [] });
  });
});
