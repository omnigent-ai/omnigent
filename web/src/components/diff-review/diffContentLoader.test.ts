import { describe, expect, it, vi } from "vitest";
import { DiffContentLoader, type DiffSourceText } from "./diffContentLoader";
import { computeFileDiff, createFileDiffComputer } from "./fileDiffComputation";

interface Deferred {
  path: string;
  fresh: boolean;
  fingerprint: string;
  signal: AbortSignal;
  resolve: (text: DiffSourceText) => void;
  reject: (error: Error) => void;
}

function harness(concurrency = 2, holdsFailure?: (cause: unknown) => boolean) {
  const requests: Deferred[] = [];
  const loader = new DiffContentLoader({
    concurrency,
    holdsFailure,
    computer: createFileDiffComputer(null),
    fetchText: (path, { fresh, fingerprint, signal }) =>
      new Promise<DiffSourceText>((resolve, reject) => {
        requests.push({ path, fresh, fingerprint, signal, resolve, reject });
      }),
  });
  const settle = () =>
    new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  const statuses = () =>
    Object.fromEntries([...loader.contents].map(([path, content]) => [path, content.status]));
  return { loader, requests, settle, statuses };
}

const files = (...paths: string[]) => paths.map((path) => ({ path, fingerprint: "1" }));

describe("DiffContentLoader", () => {
  const text = (after: string): DiffSourceText => ({ before: "base\n", after });

  it("never shows an older version's text once a newer one was listed", async () => {
    const { loader, requests, settle } = harness(2);
    loader.setFiles([{ path: "a.ts", fingerprint: "fp1" }]);
    await settle();
    loader.setFiles([{ path: "a.ts", fingerprint: "fp2" }]);
    requests[0]!.resolve(text("old fp1 text\n"));
    await settle();
    expect(loader.contents.get("a.ts")?.status).toBe("loading");
    expect(requests.map((request) => request.fingerprint)).toEqual(["fp1", "fp2"]);
    requests[1]!.resolve(text("fp2 text\n"));
    await settle();
    const content = loader.contents.get("a.ts");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual(["fp2 text\n"]);
  });

  it("keeps a held failure until Retry, through re-listings and retryFailures", async () => {
    const held = new Error("held");
    const { loader, requests, settle } = harness(2, (cause) => cause === held);
    loader.setFiles(files("a.ts", "b.ts"));
    await settle();
    requests[0]!.reject(held);
    requests[1]!.reject(new Error("transient"));
    await settle();
    expect(loader.contents.get("a.ts")?.status).toBe("failed");

    // An identical listing and an access-ready retry load only the transient failure.
    loader.setFiles(files("a.ts", "b.ts"));
    await settle();
    loader.retryFailures();
    await settle();
    expect(requests.map((request) => request.path)).toEqual(["a.ts", "b.ts", "b.ts"]);

    loader.retry("a.ts");
    await settle();
    expect(requests.map((request) => request.path)).toEqual(["a.ts", "b.ts", "b.ts", "a.ts"]);
    // A new version of a held file loads on its own.
    requests[3]!.reject(held);
    await settle();
    loader.setFiles([
      { path: "a.ts", fingerprint: "2" },
      { path: "b.ts", fingerprint: "1" },
    ]);
    await settle();
    expect(requests.at(-1)).toMatchObject({ path: "a.ts", fingerprint: "2" });
  });

  it("names the version it loads, and stops loads for dropped files and on dispose", async () => {
    const { loader, requests, settle } = harness(2);
    loader.setFiles([
      { path: "a.ts", fingerprint: "fa" },
      { path: "b.ts", fingerprint: "fb" },
    ]);
    await settle();
    expect(requests.map((request) => [request.path, request.fingerprint])).toEqual([
      ["a.ts", "fa"],
      ["b.ts", "fb"],
    ]);
    loader.setFiles([{ path: "b.ts", fingerprint: "fb" }]);
    expect(requests[0]!.signal.aborted).toBe(true);
    expect(requests[1]!.signal.aborted).toBe(false);
    loader.dispose();
    expect(requests[1]!.signal.aborted).toBe(true);
  });

  it("loads a few files at a time, with the opened file first", async () => {
    const { loader, requests, settle, statuses } = harness(2);
    loader.setFiles(files("a.ts", "b.ts", "c.ts", "d.ts"), "c.ts");
    await settle();
    expect(requests.map((request) => request.path)).toEqual(["c.ts", "a.ts"]);
    expect(statuses()).toEqual({
      "a.ts": "loading",
      "b.ts": "loading",
      "c.ts": "loading",
      "d.ts": "loading",
    });
    requests[0]!.resolve({ before: "x\n", after: "y\n" });
    await settle();
    expect(statuses()["c.ts"]).toBe("ready");
    expect(requests.map((request) => request.path)).toEqual(["c.ts", "a.ts", "b.ts"]);
  });

  it("reports sources that can't diff a file and fetch failures", async () => {
    const { loader, requests, settle } = harness(2);
    loader.setFiles(files("gone.ts", "broken.ts"));
    await settle();
    requests[0]!.resolve({ unavailable: "Original content unavailable" });
    requests[1]!.reject(new Error("503 Service Unavailable"));
    await settle();
    // The listing's fingerprint names the content, so it can be marked viewed.
    expect(loader.contents.get("gone.ts")).toEqual({
      status: "unavailable",
      message: "Original content unavailable",
      revision: "1",
    });
    expect(loader.contents.get("broken.ts")).toEqual({
      status: "failed",
      message: "Couldn't load this diff. 503 Service Unavailable",
    });

    loader.retry("broken.ts");
    await settle();
    expect(loader.contents.get("broken.ts")?.status).toBe("loading");
    requests[2]!.resolve({ before: "a\n", after: "b\n" });
    await settle();
    expect(loader.contents.get("broken.ts")?.status).toBe("ready");
  });

  it("reloads only changed files on refresh and keeps their diff until the new one lands", async () => {
    const { loader, requests, settle } = harness(4);
    loader.setFiles(files("a.ts", "b.ts"));
    await settle();
    requests.forEach((request) => request.resolve({ before: "1\n", after: "2\n" }));
    await settle();
    const shownA = loader.contents.get("a.ts");

    loader.setFiles([
      { path: "a.ts", fingerprint: "2" },
      { path: "b.ts", fingerprint: "1" },
      { path: "c.ts", fingerprint: "1" },
    ]);
    await settle();
    expect(requests.slice(2).map((request) => [request.path, request.fresh])).toEqual([
      ["a.ts", true],
      ["c.ts", false],
    ]);
    expect(loader.contents.get("a.ts")).toBe(shownA);

    // A refresh that fails keeps the diff shown, marked as out of date.
    requests[2]!.reject(new Error("timeout"));
    await settle();
    expect(loader.contents.get("a.ts")).toEqual({
      ...shownA,
      refreshError: "Couldn't load this diff. timeout",
    });

    loader.setFiles(files("c.ts"));
    await settle();
    expect([...loader.contents.keys()]).toEqual(["c.ts"]);
  });

  it("recovers from a failed refresh without another change to the file", async () => {
    const { loader, requests, settle } = harness();
    loader.setFiles(files("a.ts"));
    await settle();
    requests[0]!.resolve({ before: "1\n", after: "2\n" });
    await settle();
    // The agent's last write: a new fingerprint, then the fetch fails.
    const latest = [{ path: "a.ts", fingerprint: "2" }];
    loader.setFiles(latest);
    await settle();
    requests[1]!.reject(new Error("timeout"));
    await settle();
    expect(loader.contents.get("a.ts")).toMatchObject({
      status: "ready",
      refreshError: expect.any(String),
    });

    // The same listing again retries the latest version.
    loader.setFiles(latest);
    await settle();
    expect(requests).toHaveLength(3);
    requests[2]!.reject(new Error("timeout"));
    await settle();

    // So does Try again, which then clears the warning and shows the new diff.
    loader.retry("a.ts");
    await settle();
    expect(requests.map((request) => request.fresh)).toEqual([false, true, true, true]);
    requests[3]!.resolve({ before: "1\n", after: "3\n" });
    await settle();
    const content = loader.contents.get("a.ts");
    expect(content).toMatchObject({ status: "ready" });
    expect(content).not.toHaveProperty("refreshError");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual(["3\n"]);

    // Once fetched, an identical listing doesn't fetch again.
    loader.setFiles(latest);
    await settle();
    expect(requests).toHaveLength(4);
  });

  it("clears the out-of-date warning when a retry returns the text already shown", async () => {
    const { loader, requests, settle } = harness();
    loader.setFiles(files("a.ts"));
    await settle();
    requests[0]!.resolve({ before: "1\n", after: "2\n" });
    await settle();
    const shown = loader.contents.get("a.ts");
    loader.setFiles([{ path: "a.ts", fingerprint: "2" }]);
    await settle();
    requests[1]!.reject(new Error("timeout"));
    await settle();
    loader.retry("a.ts");
    await settle();
    requests[2]!.resolve({ before: "1\n", after: "2\n" });
    await settle();
    expect(loader.contents.get("a.ts")).toEqual(shown);
  });

  describe("when computing a diff fails", () => {
    function failingSecondCompute() {
      let computes = 0;
      const texts = new Map<string, DiffSourceText>([["a.ts", { before: "  a\n", after: "a\n" }]]);
      const loader = new DiffContentLoader({
        fetchText: async (path) => texts.get(path)!,
        computer: {
          compute: async (request) => {
            computes += 1;
            if (computes === 2) throw new Error("worker crashed");
            return computeFileDiff(request);
          },
          dispose: () => {},
        },
      });
      const settle = () =>
        new Promise((resolve) => {
          setTimeout(resolve, 0);
        });
      const shown = () => {
        const content = loader.contents.get("a.ts");
        return content?.status === "ready" ? content : undefined;
      };
      return { loader, texts, settle, shown, computes: () => computes };
    }

    it("retries the computation for refreshed text", async () => {
      const { loader, texts, settle, shown, computes } = failingSecondCompute();
      loader.setFiles(files("a.ts"));
      await settle();
      texts.set("a.ts", { before: "  a\n", after: "b\n" });
      loader.setFiles([{ path: "a.ts", fingerprint: "2" }]);
      await settle();
      expect(shown()?.refreshError).toBe("Couldn't load this diff. worker crashed");
      expect(shown()?.fileDiff.additionLines).toEqual(["a\n"]);

      loader.retry("a.ts");
      await settle();
      expect(computes()).toBe(3);
      expect(shown()?.refreshError).toBeUndefined();
      expect(shown()?.fileDiff.additionLines).toEqual(["b\n"]);
    });

    it("retries a whitespace recomputation", async () => {
      const { loader, settle, shown, computes } = failingSecondCompute();
      loader.setFiles(files("a.ts"));
      await settle();
      loader.setIgnoreWhitespace(true);
      await settle();
      expect(shown()?.refreshError).toBeDefined();
      expect(shown()?.fileDiff.hunks).toHaveLength(1);

      loader.retry("a.ts");
      await settle();
      expect(computes()).toBe(3);
      expect(shown()?.refreshError).toBeUndefined();
      expect(shown()?.fileDiff.hunks).toHaveLength(0);
    });
  });

  it.each([
    ["Try again", (loader: DiffContentLoader) => loader.retry("a.ts")],
    [
      "an identical listing",
      (loader: DiffContentLoader) => loader.setFiles([{ path: "a.ts", fingerprint: "2" }]),
    ],
  ])("restores an unavailable file after %s succeeds", async (_name, recover) => {
    const { loader, requests, settle } = harness();
    const unavailable = { unavailable: "Original content unavailable" };
    loader.setFiles(files("a.ts"));
    await settle();
    requests[0]!.resolve(unavailable);
    await settle();
    loader.setFiles([{ path: "a.ts", fingerprint: "2" }]);
    await settle();
    requests[1]!.reject(new Error("timeout"));
    await settle();
    expect(loader.contents.get("a.ts")?.status).toBe("failed");

    recover(loader);
    await settle();
    requests[2]!.resolve({ ...unavailable });
    await settle();
    expect(loader.contents.get("a.ts")).toEqual({
      status: "unavailable",
      message: "Original content unavailable",
      revision: "2",
    });
  });

  it("names an undiffable file's new revision when only its listing changes", async () => {
    const { loader, requests, settle } = harness();
    const unavailable = { unavailable: "Binary file" };
    loader.setFiles(files("a.png"));
    await settle();
    requests[0]!.resolve(unavailable);
    await settle();
    loader.setFiles([{ path: "a.png", fingerprint: "2" }]);
    await settle();
    requests[1]!.resolve({ ...unavailable });
    await settle();
    expect(loader.contents.get("a.png")).toMatchObject({ status: "unavailable", revision: "2" });
  });

  it("keeps the shown diff when a refetch returns identical text", async () => {
    const { loader, requests, settle } = harness();
    loader.setFiles(files("a.ts"));
    await settle();
    requests[0]!.resolve({ before: "1\n", after: "2\n" });
    await settle();
    const shown = loader.contents.get("a.ts");
    loader.setFiles([{ path: "a.ts", fingerprint: "2" }]);
    await settle();
    requests[1]!.resolve({ before: "1\n", after: "2\n" });
    await settle();
    expect(loader.contents.get("a.ts")).toBe(shown);
  });

  it("recomputes loaded diffs without refetching when whitespace handling changes", async () => {
    const { loader, requests, settle } = harness();
    loader.setFiles(files("a.ts"));
    await settle();
    requests[0]!.resolve({ before: "  a\n", after: "a\n" });
    await settle();
    const before = loader.contents.get("a.ts");
    expect(before?.status === "ready" && before.fileDiff.hunks.length).toBe(1);
    loader.setIgnoreWhitespace(true);
    await settle();
    expect(requests).toHaveLength(1);
    const after = loader.contents.get("a.ts");
    expect(after?.status === "ready" && after.fileDiff.hunks.length).toBe(0);
  });

  it("drops a slow load that a newer one overtook", async () => {
    const { loader, requests, settle } = harness(4);
    loader.setFiles(files("a.ts"));
    await settle();
    loader.setFiles([{ path: "a.ts", fingerprint: "2" }]);
    await settle();
    // The second request starts only after the first finishes.
    requests[0]!.resolve({ before: "old\n", after: "older\n" });
    await settle();
    requests[1]!.resolve({ before: "new\n", after: "newer\n" });
    await settle();
    const content = loader.contents.get("a.ts");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual(["newer\n"]);
  });

  it("notifies subscribers once per burst and stops after dispose", async () => {
    const { loader, requests, settle } = harness(4);
    const listener = vi.fn();
    loader.subscribe(listener);
    loader.setFiles(files("a.ts", "b.ts"));
    await settle();
    expect(listener).toHaveBeenCalledTimes(1);
    loader.dispose();
    requests.forEach((request) => request.resolve({ before: "1\n", after: "2\n" }));
    await settle();
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
