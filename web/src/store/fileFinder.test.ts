import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type FileFinderPane,
  requestFileFinder,
  requestTextSearch,
  useFileFinderHost,
  useFileFinderPane,
  useFileFinderStore,
} from "./fileFinder";

const store = () => useFileFinderStore.getState();

beforeEach(() => {
  useFileFinderStore.setState({ availableSessionId: null, request: null, pane: null });
});

describe("requestFileFinder", () => {
  it("records a request for the session whose workspace the finder can search", () => {
    store().setAvailableSession("a");
    expect(requestFileFinder("a", { query: "x" })).toBe(true);
    expect(store().request).toMatchObject({ sessionId: "a", query: "x" });
  });

  it("refuses a session the finder can't search", () => {
    expect(requestFileFinder("a")).toBe(false);
    store().setAvailableSession("b");
    expect(requestFileFinder("a")).toBe(false);
    expect(store().request).toBeNull();
  });

  it("keeps a newer request when an older one finishes", () => {
    store().setAvailableSession("a");
    requestFileFinder("a");
    const first = store().request!.id;
    requestFileFinder("a", { toggle: true });
    store().finishRequest(first);
    expect(store().request).toMatchObject({ toggle: true });
    store().finishRequest(store().request!.id);
    expect(store().request).toBeNull();
  });
});

describe("useFileFinderHost", () => {
  it("offers its session and shows each request once", () => {
    const show = vi.fn();
    renderHook(() => useFileFinderHost("a", show));
    expect(store().availableSessionId).toBe("a");

    act(() => void requestFileFinder("a", { query: "r" }));
    expect(show).toHaveBeenCalledTimes(1);
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ sessionId: "a", query: "r" }));
    expect(store().request).toBeNull();

    act(() => void requestFileFinder("a", { toggle: true }));
    expect(show).toHaveBeenCalledTimes(2);
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ toggle: true }));
  });

  it("follows the open session and stops offering one when there's none", () => {
    const show = vi.fn();
    const { rerender } = renderHook(({ sessionId }) => useFileFinderHost(sessionId, show), {
      initialProps: { sessionId: "a" as string | null },
    });
    rerender({ sessionId: "b" });
    expect(requestFileFinder("a")).toBe(false);
    act(() => void requestFileFinder("b"));
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ sessionId: "b" }));

    rerender({ sessionId: null });
    expect(store().availableSessionId).toBeNull();
    expect(requestFileFinder("b")).toBe(false);
  });

  it("drops a request left for a session that's no longer open", () => {
    const show = vi.fn();
    useFileFinderStore.setState({ request: { id: 99, sessionId: "old", kind: "files" } });
    renderHook(() => useFileFinderHost("a", show));
    expect(show).not.toHaveBeenCalled();
    expect(store().request).toBeNull();
  });
});

describe("useFileFinderPane", () => {
  const pane = (sessionId: string, openFile = vi.fn()): FileFinderPane => ({
    sessionId,
    hostId: "host",
    openFile,
  });

  it("offers one stable pane per session that opens with the latest callback", () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = renderHook(({ offer }) => useFileFinderPane(offer), {
      initialProps: { offer: pane("a", first) as FileFinderPane | null },
    });
    const offered = store().pane;
    expect(offered).toMatchObject({ sessionId: "a", hostId: "host" });

    rerender({ offer: pane("a", latest) });
    expect(store().pane).toBe(offered);
    store().pane!.openFile("src/x.ts", { line: 3 });
    expect(latest).toHaveBeenCalledWith("src/x.ts", { line: 3 });
    expect(first).not.toHaveBeenCalled();

    rerender({ offer: pane("b") });
    expect(store().pane?.sessionId).toBe("b");
    rerender({ offer: null });
    expect(store().pane).toBeNull();
  });

  it("leaves a newly selected pane in place when the previous one withdraws", () => {
    const previous = renderHook(() => useFileFinderPane(pane("a")));
    renderHook(() => useFileFinderPane(pane("b")));
    previous.unmount();
    expect(store().pane?.sessionId).toBe("b");
  });
});

describe("requestTextSearch", () => {
  it("asks the shell serving the session to search its contents", () => {
    store().setAvailableSession("a");
    expect(requestTextSearch("a")).toBe(true);
    expect(store().request).toMatchObject({ sessionId: "a", kind: "text" });
  });

  it("carries a query, an empty one too, as a new request each time", () => {
    store().setAvailableSession("a");
    requestTextSearch("a", { query: "needle" });
    const first = store().request!;
    expect(first).toMatchObject({ sessionId: "a", kind: "text", query: "needle" });
    requestTextSearch("a", { query: "needle" });
    expect(store().request!.id).not.toBe(first.id);
    requestTextSearch("a", { query: "" });
    expect(store().request).toMatchObject({ kind: "text", query: "" });
  });

  it("refuses a session no shell serves", () => {
    expect(requestTextSearch("a")).toBe(false);
    store().setAvailableSession("b");
    expect(requestTextSearch("a")).toBe(false);
    expect(store().request).toBeNull();
  });

  it("reaches the host as a text request, and a finder request stays a file request", () => {
    const show = vi.fn();
    renderHook(() => useFileFinderHost("a", show));
    act(() => {
      requestTextSearch("a");
    });
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "text" }));
    act(() => {
      requestFileFinder("a", { query: "x" });
    });
    expect(show).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "files", query: "x" }));
  });
});
