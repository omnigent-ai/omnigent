import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  baseIntentSource,
  readWorkingCopyBaseIntent,
  resetFolderBaseIntentsForTests,
  subscribeWorkingCopyBaseIntent,
  useBaseIntent,
  useWorkingCopyBaseIntent,
  writeWorkingCopyBaseIntent,
} from "./workingCopyBasePreference";

const STORAGE_KEY = "omnigent.changes.base";

afterEach(() => {
  window.localStorage.clear();
  resetFolderBaseIntentsForTests();
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

describe("a repository folder's base", () => {
  const FOLDER = ".worktrees/feature";

  it("follows the workspace's preference until a base is chosen in the folder", () => {
    const folder = baseIntentSource(FOLDER, "host_1\n/repo");
    writeWorkingCopyBaseIntent("branch-point");
    expect(folder.read()).toBe("branch-point");
    folder.write("head");
    expect(folder.read()).toBe("head");
    writeWorkingCopyBaseIntent("automatic");
    expect(folder.read()).toBe("head");
  });

  it("changes neither the workspace's preference nor a same-named folder elsewhere", () => {
    baseIntentSource(FOLDER, "host_1\n/repo").write("head");
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(baseIntentSource(FOLDER, "host_1\n/other").read()).toBe("automatic");
    expect(baseIntentSource(FOLDER, "host_2\n/repo").read()).toBe("automatic");
    expect(baseIntentSource("", "host_1\n/repo").read()).toBe("automatic");
  });

  it("keeps a folder's review current with its own choice and the workspace's", () => {
    const { result } = renderHook(() => useBaseIntent(FOLDER, "host_1\n/repo"));
    expect(result.current).toBe("automatic");
    act(() => writeWorkingCopyBaseIntent("branch-point"));
    expect(result.current).toBe("branch-point");
    act(() => baseIntentSource(FOLDER, "host_1\n/repo").write("head"));
    expect(result.current).toBe("head");
  });
});
