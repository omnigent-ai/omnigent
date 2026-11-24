import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { QuickPickItem, QuickPickLoadRequest, QuickPickSource } from "./model";
import { SOURCE_LOAD_ERROR_MESSAGE, useQuickPickSources } from "./useQuickPickSources";

function item(id: string): QuickPickItem<string> {
  return { id, label: id, value: id };
}

interface Deferred {
  request: QuickPickLoadRequest<string>;
  resolve: (items: QuickPickItem<string>[]) => void;
  reject: (error: unknown) => void;
}

function deferredSource(overrides: Partial<QuickPickSource<string>> = {}) {
  const calls: Deferred[] = [];
  const source: QuickPickSource<string> = {
    id: "async",
    queryDependent: true,
    load: (request) =>
      new Promise((resolve, reject) => {
        calls.push({ request, resolve, reject });
      }),
    ...overrides,
  };
  return { source, calls };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useQuickPickSources", () => {
  it("resolves synchronous sources on the first render without a loading state", () => {
    const source: QuickPickSource<string> = { id: "sync", load: () => [item("a")] };
    const { result } = renderHook(() => useQuickPickSources([source], ""));
    expect(result.current.states.sync).toMatchObject({ status: "ready", items: [item("a")] });
  });

  it("waits for a promise that isn't the global Promise, as when telemetry patches it", async () => {
    let resolve!: (items: QuickPickItem<string>[]) => void;
    const native = new Promise<QuickPickItem<string>[]>((settle) => {
      resolve = settle;
    });
    // Zone-based tracing swaps `window.Promise`, so a native promise fails `instanceof Promise`.
    const foreign: PromiseLike<QuickPickItem<string>[]> = { then: native.then.bind(native) };
    expect(foreign instanceof Promise).toBe(false);
    const source = {
      id: "foreign",
      load: () => foreign,
    } as unknown as QuickPickSource<string>;
    const { result } = renderHook(() => useQuickPickSources([source], ""));
    expect(result.current.states.foreign).toMatchObject({ status: "loading", items: [] });
    await act(async () => resolve([item("a")]));
    expect(result.current.states.foreign).toMatchObject({ status: "ready", items: [item("a")] });
  });

  it("aborts a stale request and ignores its late response", async () => {
    const { source, calls } = deferredSource();
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "a" },
    });
    expect(result.current.states.async.status).toBe("loading");
    rerender({ query: "ab" });
    expect(calls[0].request.signal.aborted).toBe(true);

    await act(async () => calls[1].resolve([item("fresh")]));
    await act(async () => calls[0].resolve([item("stale")]));
    expect(result.current.states.async).toMatchObject({ status: "ready", items: [item("fresh")] });
  });

  it("keeps previous results visible while a reload is pending", async () => {
    const { source, calls } = deferredSource();
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "a" },
    });
    await act(async () => calls[0].resolve([item("first")]));
    rerender({ query: "ab" });
    expect(result.current.states.async).toMatchObject({
      status: "loading",
      items: [item("first")],
    });
  });

  it("hides a provider-ranked source's rows for an old query while the new one loads or fails", async () => {
    vi.useFakeTimers();
    const { source, calls } = deferredSource({ ranking: "provider", debounceMs: 300 });
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "parser" },
    });
    await act(async () => calls[0].resolve([item("old thread")]));
    rerender({ query: "no match" });
    expect(result.current.states.async).toMatchObject({ status: "loading", items: [] });
    await act(async () => vi.advanceTimersByTime(300));
    await act(async () => calls[1].reject(new Error("offline")));
    expect(result.current.states.async).toMatchObject({ status: "error", items: [] });
  });

  it("keeps a provider-ranked source's rows while the same query reloads", async () => {
    const { source, calls } = deferredSource({ ranking: "provider", reloadKey: "1" });
    const { result, rerender } = renderHook(
      ({ reloadKey }) => useQuickPickSources([{ ...source, reloadKey }], "parser"),
      { initialProps: { reloadKey: "1" } },
    );
    await act(async () => calls[0].resolve([item("first")]));
    rerender({ reloadKey: "2" });
    expect(result.current.states.async).toMatchObject({
      status: "loading",
      items: [item("first")],
    });
  });

  it("hides rows for other options at once but reloads after the debounce", async () => {
    vi.useFakeTimers();
    const { source, calls } = deferredSource({
      ranking: "provider",
      debounceMs: 300,
      resultKey: "",
    });
    const { result, rerender } = renderHook(
      ({ resultKey }) => useQuickPickSources([{ ...source, resultKey }], "parser"),
      { initialProps: { resultKey: "" } },
    );
    await act(async () => calls[0].resolve([item("first")]));
    for (const resultKey of ["n", "no", "nod", "node/"]) rerender({ resultKey });
    expect(result.current.states.async).toMatchObject({ status: "loading", items: [] });
    expect(calls).toHaveLength(1);
    await act(async () => vi.advanceTimersByTime(300));
    expect(calls).toHaveLength(2);
    await act(async () => calls[1].reject(new Error("offline")));
    expect(result.current.states.async).toMatchObject({ status: "error", items: [] });
  });

  it("does not reload a query-independent source when the query changes", () => {
    const load = vi.fn(() => [item("a")]);
    const { rerender } = renderHook(
      ({ query }) => useQuickPickSources([{ id: "s", load }], query),
      {
        initialProps: { query: "" },
      },
    );
    rerender({ query: "x" });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("reloads when the reload key changes even with a new source object each render", () => {
    const load = vi.fn(() => [item("a")]);
    const { rerender } = renderHook(
      ({ reloadKey }) => useQuickPickSources([{ id: "s", load, reloadKey }], ""),
      { initialProps: { reloadKey: "case=off" } },
    );
    rerender({ reloadKey: "case=off" });
    expect(load).toHaveBeenCalledTimes(1);
    rerender({ reloadKey: "case=on" });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("debounces query-dependent reloads after the first load", () => {
    vi.useFakeTimers();
    const { source, calls } = deferredSource({ debounceMs: 100 });
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "" },
    });
    expect(calls).toHaveLength(1);
    rerender({ query: "a" });
    rerender({ query: "ab" });
    expect(calls).toHaveLength(1);
    expect(result.current.states.async.status).toBe("loading");
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].request.query).toBe("ab");
  });

  it("reports errors, ignores aborts, and retries failed sources", async () => {
    const { source, calls } = deferredSource();
    const { result } = renderHook(() => useQuickPickSources([source], "a"));
    await act(async () => calls[0].reject(new Error("GitHub is unreachable")));
    expect(result.current.states.async).toMatchObject({
      status: "error",
      error: "GitHub is unreachable",
    });

    act(() => result.current.retry());
    expect(calls).toHaveLength(2);
    expect(result.current.states.async.status).toBe("loading");
    await act(async () => calls[1].resolve([item("ok")]));
    expect(result.current.states.async).toMatchObject({ status: "ready", items: [item("ok")] });
  });

  it("ignores an AbortError from a request a newer query superseded", async () => {
    const { source, calls } = deferredSource();
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "a" },
    });
    rerender({ query: "ab" });
    await act(async () => calls[0].reject(new DOMException("Aborted", "AbortError")));
    expect(result.current.states.async.status).toBe("loading");
    await act(async () => calls[1].resolve([item("ab")]));
    expect(result.current.states.async).toMatchObject({ status: "ready", items: [item("ab")] });
  });

  it("fails a live request whose loader gave up with an AbortError, and retries it", async () => {
    const { source, calls } = deferredSource();
    const { result } = renderHook(() => useQuickPickSources([source], "a"));
    await act(async () => calls[0].reject(new DOMException("Provider cancelled", "AbortError")));
    expect(result.current.states.async).toMatchObject({
      status: "error",
      error: SOURCE_LOAD_ERROR_MESSAGE,
    });
    act(() => result.current.retry());
    expect(calls).toHaveLength(2);
    await act(async () => calls[1].resolve([item("ok")]));
    expect(result.current.states.async).toMatchObject({ status: "ready", items: [item("ok")] });
  });

  it("shows published snapshots while loading and the settled value once complete", async () => {
    const { source, calls } = deferredSource();
    const { result } = renderHook(() => useQuickPickSources([source], "a"));
    act(() => calls[0].request.publish([item("first")]));
    expect(result.current.states.async).toMatchObject({
      status: "loading",
      items: [item("first")],
    });
    act(() => calls[0].request.publish([item("first"), item("second")]));
    expect(result.current.states.async.items).toEqual([item("first"), item("second")]);
    await act(async () => calls[0].resolve([item("first"), item("second"), item("third")]));
    expect(result.current.states.async).toMatchObject({ status: "ready" });
    expect(result.current.states.async.items).toHaveLength(3);
  });

  it("ignores snapshots from a replaced query and after completion", async () => {
    const { source, calls } = deferredSource();
    const { result, rerender } = renderHook(({ query }) => useQuickPickSources([source], query), {
      initialProps: { query: "a" },
    });
    rerender({ query: "ab" });
    act(() => calls[0].request.publish([item("stale")]));
    expect(result.current.states.async.items).toEqual([]);

    act(() => calls[1].request.publish([item("fresh")]));
    await act(async () => calls[1].resolve([item("final")]));
    act(() => calls[1].request.publish([item("late")]));
    expect(result.current.states.async).toMatchObject({ status: "ready", items: [item("final")] });
  });

  it("keeps a snapshot published synchronously before the promise", () => {
    const source: QuickPickSource<string> = {
      id: "eager",
      load: ({ publish }) => {
        publish([item("cached")]);
        return new Promise(() => {});
      },
    };
    const { result } = renderHook(() => useQuickPickSources([source], ""));
    expect(result.current.states.eager).toMatchObject({
      status: "loading",
      items: [item("cached")],
    });
  });

  it("uses a generic message when a failure has no message", () => {
    const source: QuickPickSource<string> = {
      id: "throws",
      load: () => {
        throw new Error("");
      },
    };
    const { result } = renderHook(() => useQuickPickSources([source], ""));
    expect(result.current.states.throws.error).toBe(SOURCE_LOAD_ERROR_MESSAGE);
  });

  it("aborts in-flight requests on unmount and when a source is removed", () => {
    const first = deferredSource({ id: "first" });
    const second = deferredSource({ id: "second" });
    const { result, rerender, unmount } = renderHook(
      ({ sources }) => useQuickPickSources(sources, ""),
      { initialProps: { sources: [first.source, second.source] } },
    );
    rerender({ sources: [second.source] });
    expect(first.calls[0].request.signal.aborted).toBe(true);
    expect(result.current.states.first).toBeUndefined();
    unmount();
    expect(second.calls[0].request.signal.aborted).toBe(true);
  });
});
