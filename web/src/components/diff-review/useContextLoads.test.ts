import { act, renderHook } from "@testing-library/react";
import { parsePatchFiles, type FileDiffLoadedFiles, type FileDiffMetadata } from "@pierre/diffs";
import { describe, expect, it, vi } from "vitest";
import { MismatchedFullTextError, StaleContextLoadError, useContextLoads } from "./useContextLoads";

const patchFor = (path: string) =>
  parsePatchFiles(
    [
      `diff --git a/${path} b/${path}`,
      `--- a/${path}`,
      `+++ b/${path}`,
      "@@ -5,3 +5,3 @@",
      " line 5",
      "-line 6",
      "+line 6 changed",
      " line 7",
      "",
    ].join("\n"),
  )[0]!.files[0]!;
const files = (path: string): FileDiffLoadedFiles => {
  const lines = Array.from({ length: 20 }, (_, index) => `line ${index + 1}`);
  return {
    oldFile: { name: path, contents: `${lines.join("\n")}\n` },
    newFile: {
      name: path,
      contents: `${lines.map((line) => (line === "line 6" ? "line 6 changed" : line)).join("\n")}\n`,
    },
  };
};

function deferred() {
  const pending = new Map<FileDiffMetadata, (files: FileDiffLoadedFiles) => void>();
  const failures = new Map<FileDiffMetadata, (error: Error) => void>();
  const load = vi.fn(
    (fileDiff: FileDiffMetadata) =>
      new Promise<FileDiffLoadedFiles>((resolve, reject) => {
        pending.set(fileDiff, resolve);
        failures.set(fileDiff, reject);
      }),
  );
  return {
    load,
    finish: (fileDiff: FileDiffMetadata) => pending.get(fileDiff)?.(files(fileDiff.name)),
    fail: (fileDiff: FileDiffMetadata) => failures.get(fileDiff)?.(new Error("no answer")),
  };
}

describe("useContextLoads", () => {
  it("keeps each file's state to itself and loads each once at a time", async () => {
    const first = patchFor("a.ts");
    const second = patchFor("b.ts");
    const loader = deferred();
    const { result } = renderHook(() => useContextLoads(loader.load));
    let loading!: Promise<FileDiffLoadedFiles>;
    act(() => {
      loading = result.current.load!(first);
      void result.current.load!(first);
      void result.current.load!(second).catch(() => undefined);
    });
    expect(loader.load).toHaveBeenCalledTimes(2);
    expect(result.current.states.get(first)).toBe("loading");
    expect(result.current.states.get(second)).toBe("loading");

    await act(async () => {
      loader.finish(first);
      await loading;
    });
    expect(result.current.states.get(first)).toBeUndefined();
    expect(result.current.hydrated(first)?.isPartial).toBe(false);
    expect(result.current.states.get(second)).toBe("loading");

    await act(async () => loader.fail(second));
    expect(result.current.states.get(second)).toBe("failed");
    expect(result.current.hydrated(second)).toBeUndefined();
  });

  it("loads a failed file again on Retry", async () => {
    const fileDiff = patchFor("a.ts");
    const load = vi
      .fn<(fileDiff: FileDiffMetadata) => Promise<FileDiffLoadedFiles>>()
      .mockRejectedValueOnce(new Error("no answer"))
      .mockResolvedValue(files("a.ts"));
    const { result } = renderHook(() => useContextLoads(load));
    await act(async () => {
      await result.current.load!(fileDiff).catch(() => undefined);
    });
    expect(result.current.states.get(fileDiff)).toBe("failed");
    await act(async () => {
      result.current.retry(fileDiff);
    });
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.states.get(fileDiff)).toBeUndefined();
    expect(result.current.hydrated(fileDiff)).toBeDefined();
  });

  it("keeps a file's patch when its full text doesn't match it, and doesn't load it again", async () => {
    const fileDiff = patchFor("a.ts");
    // An empty side, as a provider gives for a side it couldn't read.
    const load = vi.fn(async (): Promise<FileDiffLoadedFiles> => ({
      oldFile: files("a.ts").oldFile,
      newFile: { name: "a.ts", contents: "" },
    }));
    const { result } = renderHook(() => useContextLoads(load));
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.load!(fileDiff).catch((error: unknown) => error);
    });
    expect(outcome).toBeInstanceOf(MismatchedFullTextError);
    expect(result.current.states.get(fileDiff)).toBe("mismatched");
    expect(result.current.hydrated(fileDiff)).toBeUndefined();
    await act(async () => {
      result.current.retry(fileDiff);
      await result.current.load!(fileDiff).catch(() => undefined);
    });
    expect(load).toHaveBeenCalledOnce();
    expect(result.current.states.get(fileDiff)).toBe("mismatched");
  });

  it("rejects what a replaced loader finishes and forgets its state", async () => {
    const fileDiff = patchFor("a.ts");
    const old = deferred();
    const { result, rerender } = renderHook(({ load }) => useContextLoads(load), {
      initialProps: { load: old.load },
    });
    let stale!: Promise<FileDiffLoadedFiles>;
    act(() => {
      stale = result.current.load!(fileDiff);
    });
    const next = deferred();
    rerender({ load: next.load });
    expect(result.current.states.size).toBe(0);
    old.finish(fileDiff);
    await expect(stale).rejects.toBeInstanceOf(StaleContextLoadError);
    expect(result.current.states.size).toBe(0);
    expect(result.current.hydrated(fileDiff)).toBeUndefined();
    // The new loader loads the same patch afresh.
    act(() => {
      void result.current.load!(fileDiff);
    });
    expect(next.load).toHaveBeenCalledTimes(1);
  });

  it("rejects what finishes after the view closed", async () => {
    const fileDiff = patchFor("a.ts");
    const loader = deferred();
    const { result, unmount } = renderHook(() => useContextLoads(loader.load));
    let loading!: Promise<FileDiffLoadedFiles>;
    act(() => {
      loading = result.current.load!(fileDiff);
    });
    unmount();
    loader.finish(fileDiff);
    await expect(loading).rejects.toBeInstanceOf(StaleContextLoadError);
  });
});
