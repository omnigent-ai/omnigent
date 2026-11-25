import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  readWorkingCopyBaseIntent,
  subscribeWorkingCopyBaseIntent,
  useWorkingCopyBaseIntent,
  writeWorkingCopyBaseIntent,
} from "./workingCopyBasePreference";

const STORAGE_KEY = "omnigent.changes.base";

afterEach(() => {
  window.localStorage.clear();
});

describe("working copy base preference", () => {
  it("is Automatic until a base is chosen, and for anything it can't read", () => {
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
    window.localStorage.setItem(STORAGE_KEY, "upstream");
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
  });

  it("stores a choice and stores Automatic as no choice", () => {
    expect(writeWorkingCopyBaseIntent("branch-point")).toBe(true);
    expect(readWorkingCopyBaseIntent()).toBe("branch-point");
    writeWorkingCopyBaseIntent("automatic");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("tells listeners in this window and hears other windows", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeWorkingCopyBaseIntent(listener);
    writeWorkingCopyBaseIntent("head");
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated" }));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    writeWorkingCopyBaseIntent("automatic");
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("keeps a hook live", () => {
    const { result } = renderHook(() => useWorkingCopyBaseIntent());
    expect(result.current).toBe("automatic");
    act(() => {
      writeWorkingCopyBaseIntent("head");
    });
    expect(result.current).toBe("head");
  });
});
