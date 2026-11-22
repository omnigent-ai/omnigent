import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  readDiffFileOrder,
  resetUnsavedDiffFileOrder,
  useDiffFileOrder,
  writeDiffFileOrder,
} from "./diffFileOrderPreference";

const KEY = "omnigent.diffReview.fileOrder";

afterEach(() => {
  window.localStorage.removeItem(KEY);
  resetUnsavedDiffFileOrder();
  vi.restoreAllMocks();
});

describe("diff file order preference", () => {
  it("orders by path until a reader chooses, and reads anything unknown as by path", () => {
    expect(readDiffFileOrder()).toBe("path");
    window.localStorage.setItem(KEY, "newest");
    expect(readDiffFileOrder()).toBe("path");
  });

  it("keeps the choice on this device", () => {
    writeDiffFileOrder("size");
    expect(window.localStorage.getItem(KEY)).toBe("size");
    expect(readDiffFileOrder()).toBe("size");
  });

  it("keeps the choice until reload where storage refuses it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    writeDiffFileOrder("size");
    expect(readDiffFileOrder()).toBe("size");
  });

  it("changes every open review at once, and follows another window's choice", () => {
    const first = renderHook(() => useDiffFileOrder());
    const second = renderHook(() => useDiffFileOrder());
    act(() => first.result.current[1]("size"));
    expect(second.result.current[0]).toBe("size");
    act(() => {
      window.localStorage.setItem(KEY, "path");
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
    });
    expect(first.result.current[0]).toBe("path");
  });

  it("follows another window's choice after a write here failed, and its clearing storage", () => {
    const review = renderHook(() => useDiffFileOrder());
    const failWrite = () => {
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new Error("quota");
      });
      act(() => review.result.current[1]("size"));
      setItem.mockRestore();
      expect(review.result.current[0]).toBe("size");
    };

    failWrite();
    act(() => {
      window.localStorage.setItem(KEY, "path");
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
    });
    expect(review.result.current[0]).toBe("path");

    failWrite();
    act(() => {
      window.localStorage.clear();
      window.dispatchEvent(new StorageEvent("storage", { key: null }));
    });
    expect(review.result.current[0]).toBe("path");
    expect(renderHook(() => useDiffFileOrder()).result.current[0]).toBe("path");
  });
});
