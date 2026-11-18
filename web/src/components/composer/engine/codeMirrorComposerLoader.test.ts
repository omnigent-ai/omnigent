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
});
