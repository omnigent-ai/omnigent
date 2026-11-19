import { afterEach, describe, expect, it, vi } from "vitest";
import * as CodeMirrorComposerModule from "./codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
  subscribeCodeMirrorComposerLoad,
} from "./codeMirrorComposerLoader";

afterEach(() => {
  resetCodeMirrorComposerLoadForTests();
  vi.restoreAllMocks();
});

describe("loading the CodeMirror composer", () => {
  it("goes idle, loading, then ready, and tells subscribers at each step", async () => {
    let finishImport!: () => void;
    resetCodeMirrorComposerLoadForTests(
      () =>
        new Promise((resolve) => {
          finishImport = () => resolve(CodeMirrorComposerModule);
        }),
    );
    const statuses: string[] = [];
    const unsubscribe = subscribeCodeMirrorComposerLoad(() =>
      statuses.push(codeMirrorComposerLoad().status),
    );
    expect(codeMirrorComposerLoad().status).toBe("idle");

    loadCodeMirrorComposer();
    expect(codeMirrorComposerLoad().status).toBe("loading");
    loadCodeMirrorComposer();

    await vi.waitFor(() => expect(finishImport).toBeDefined());
    finishImport();
    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    expect(statuses).toEqual(["loading", "ready"]);
    unsubscribe();
  });

  it("settles failed when the chunk can't load, and retries on the next request", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const importer = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(CodeMirrorComposerModule);
    resetCodeMirrorComposerLoadForTests(importer);

    loadCodeMirrorComposer();
    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("failed"));
    expect(warn).toHaveBeenCalledTimes(1);

    loadCodeMirrorComposer();
    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    expect(importer).toHaveBeenCalledTimes(2);
  });

  it("settles failed when the importer throws synchronously, and retries", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const importer = vi
      .fn()
      .mockImplementationOnce(() => {
        throw new Error("sync failure");
      })
      .mockResolvedValueOnce(CodeMirrorComposerModule);
    resetCodeMirrorComposerLoadForTests(importer);

    loadCodeMirrorComposer();
    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("failed"));
    expect(warn).toHaveBeenCalledTimes(1);

    loadCodeMirrorComposer();
    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    expect(importer).toHaveBeenCalledTimes(2);
  });

  it("settles failed when the editor factory throws", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    resetCodeMirrorComposerLoadForTests(() =>
      Promise.resolve({
        createCodeMirrorComposerInput: () => {
          throw new Error("no editor");
        },
      }),
    );

    loadCodeMirrorComposer();

    await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("failed"));
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it.each(["loading", "ready", "failed"] as const)(
    "keeps a loaded editor when a subscriber throws on %s",
    async (throwOn) => {
      vi.spyOn(console, "warn").mockImplementation(() => {});
      const logError = vi.spyOn(console, "error").mockImplementation(() => {});
      const importer = vi
        .fn()
        .mockImplementationOnce(() =>
          throwOn === "failed"
            ? Promise.reject(new Error("offline"))
            : Promise.resolve(CodeMirrorComposerModule),
        )
        .mockResolvedValue(CodeMirrorComposerModule);
      resetCodeMirrorComposerLoadForTests(importer);
      const throwingListener = subscribeCodeMirrorComposerLoad(() => {
        if (codeMirrorComposerLoad().status === throwOn) throw new Error("listener failed");
      });
      const statuses: string[] = [];
      const healthyListener = subscribeCodeMirrorComposerLoad(() =>
        statuses.push(codeMirrorComposerLoad().status),
      );

      loadCodeMirrorComposer();
      const settled = throwOn === "failed" ? "failed" : "ready";
      await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe(settled));
      if (throwOn === "failed") {
        loadCodeMirrorComposer();
        await vi.waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
      }

      expect(importer).toHaveBeenCalledTimes(throwOn === "failed" ? 2 : 1);
      expect(logError).toHaveBeenCalledTimes(1);
      expect(statuses).toEqual(
        throwOn === "failed" ? ["loading", "failed", "loading", "ready"] : ["loading", "ready"],
      );
      throwingListener();
      healthyListener();
    },
  );

  it("drops a load that finishes after a reset", async () => {
    let finishImport!: () => void;
    resetCodeMirrorComposerLoadForTests(
      () =>
        new Promise((resolve) => {
          finishImport = () => resolve(CodeMirrorComposerModule);
        }),
    );
    loadCodeMirrorComposer();

    await vi.waitFor(() => expect(finishImport).toBeDefined());
    resetCodeMirrorComposerLoadForTests();
    finishImport();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });

    expect(codeMirrorComposerLoad().status).toBe("idle");
  });
});
