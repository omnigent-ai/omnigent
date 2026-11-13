// The shared pool at the real CodeView: Pierre's own pool manager on workers the test controls.
// While the shared pool starts, fails or never answers, a review shows its diff with a pool of its
// own, as before sharing, never the shared one; once it's ready, the next review takes it.

import type { FileDiffMetadata } from "@pierre/diffs";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type * as DiffReviewModule from "./DiffReview";
import type * as DiffReviewModelModule from "./diffReviewModel";
import type * as FileDiffComputationModule from "./fileDiffComputation";
import type * as SharedDiffWorkersModule from "./sharedDiffWorkers";

/** How the test's workers answer Pierre's start-up request: never, with an error, or at once. */
const workers = vi.hoisted(() => ({
  behaviour: "silent" as "silent" | "error" | "answer",
  made: [] as FakeWorker[],
  rendered: [] as string[],
}));

class FakeWorker extends EventTarget {
  // Each worker keeps how it was made to answer, so the shared pool's stay silent.
  private readonly behaviour = workers.behaviour;
  constructor() {
    super();
    workers.made.push(this);
  }
  private renderOptions: RenderOptions | undefined;
  postMessage(request: WorkerRequest) {
    if (request.type === "diff" && this.behaviour === "answer") {
      void this.renderDiff(request);
      return;
    }
    if (request.type !== "initialize") return;
    this.renderOptions = request.renderOptions;
    if (this.behaviour === "error") {
      queueMicrotask(() => this.dispatchEvent(new Event("error")));
    } else if (this.behaviour === "answer") {
      queueMicrotask(() =>
        this.dispatchEvent(
          new MessageEvent("message", {
            data: { type: "success", requestType: "initialize", id: request.id },
          }),
        ),
      );
    }
  }
  terminate() {}

  // Renders the diff as Pierre's worker would, in plain text, so the pool caches a real result.
  private async renderDiff(request: WorkerRequest) {
    const pierre = await import("@pierre/diffs");
    const options = this.renderOptions!;
    const theme = options.theme;
    const highlighter = await pierre.getSharedHighlighter({
      themes: typeof theme === "string" ? [theme] : Object.values(theme),
      langs: ["text"],
    });
    const result = pierre.renderDiffWithHighlighter(request.diff!, highlighter, options as never, {
      forcePlainText: true,
    });
    workers.rendered.push(request.diff!.cacheKey ?? "");
    this.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "success", requestType: "diff", id: request.id, result, options },
      }),
    );
  }
}

type RenderOptions = { theme: string | Record<string, string> } & Record<string, unknown>;
interface WorkerRequest {
  id?: string;
  type?: string;
  renderOptions?: RenderOptions;
  diff?: FileDiffMetadata;
}

let DiffReview: typeof DiffReviewModule.DiffReview;
let shared: NonNullable<typeof SharedDiffWorkersModule.sharedDiffWorkers>;
let reviewFile: typeof DiffReviewModelModule.reviewFile;
let computeFileDiff: typeof FileDiffComputationModule.computeFileDiff;

const PATH = "src/app.ts";

beforeAll(() => {
  Element.prototype.scrollTo ??= function scrollTo() {};
  Element.prototype.scrollBy ??= function scrollBy() {};
});

beforeEach(async () => {
  workers.behaviour = "silent";
  workers.made = [];
  workers.rendered = [];
  vi.resetModules();
  vi.stubGlobal("Worker", FakeWorker);
  ({ DiffReview } = await import("./DiffReview"));
  ({ reviewFile } = await import("./diffReviewModel"));
  ({ computeFileDiff } = await import("./fileDiffComputation"));
  shared = (await import("./sharedDiffWorkers")).sharedDiffWorkers!;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function review(
  change: { before: string | null; after: string | null; revision: string } = {
    before: "const one = 1;\n",
    after: "const one = 1;\nconst two = 2;\n",
    revision: "r1",
  },
) {
  const fileDiff = computeFileDiff({ path: PATH, ...change, ignoreWhitespace: false });
  return (
    <DiffReview
      diagnosticsView="working_copy"
      files={[reviewFile(PATH, change.after === null ? "deleted" : "modified")]}
      contents={new Map([[PATH, { status: "ready", fileDiff }]])}
      onRetry={() => undefined}
      layout="unified"
      wrapLines={false}
      findOpen={false}
      findFocusRequest={0}
      onFindClose={() => undefined}
      emptyMessage="No changes"
    />
  );
}

/** The diff's own text, once CodeView has drawn it in the file's shadow root. */
const drawn = () => document.querySelector("diffs-container")?.shadowRoot?.textContent ?? "";

describe("a review while the shared pool starts or fails", () => {
  it("shows its diff with its own workers while the shared pool's start never answers", async () => {
    shared.start();
    expect(shared.status()).toBe("starting");
    await waitFor(() => expect(workers.made.length).toBeGreaterThan(0));
    // The review's own workers answer; the shared pool's, started above, never do.
    workers.behaviour = "answer";
    render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    expect(shared.peekReady()).toBeNull();
  });

  it("shows its diff after a worker fails before the pool started", async () => {
    workers.behaviour = "error";
    shared.start();
    await waitFor(() => expect(shared.status()).toBe("failed"));
    workers.behaviour = "answer";
    render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
  });

  it("shows its diff after the pool's start timed out", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    shared.start();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(shared.status()).toBe("failed");
    vi.useRealTimers();
    workers.behaviour = "answer";
    render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
  });

  it("gives up on a ready pool whose worker fails, and hands the next review a fresh one", async () => {
    workers.behaviour = "answer";
    shared.start();
    await waitFor(() => expect(shared.status()).toBe("ready"));
    const broken = shared.peekReady()!;
    const first = render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    expect(document.querySelector("[data-diff-workers]")?.getAttribute("data-diff-workers")).toBe(
      "shared",
    );
    // One of its workers fails after the pool started: Pierre only logs it.
    act(() => {
      workers.made[0]!.dispatchEvent(new Event("error"));
    });
    expect(shared.status()).toBe("failed");
    expect(shared.peekReady()).toBeNull();
    // The open review keeps its view; the next one starts its own while a fresh pool starts.
    expect(drawn()).toContain("const two = 2;");
    first.unmount();
    shared.start();
    await waitFor(() => expect(shared.status()).toBe("ready"));
    const fresh = shared.peekReady();
    expect(fresh).not.toBeNull();
    expect(fresh).not.toBe(broken);
    render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    expect(document.querySelector("[data-diff-workers]")?.getAttribute("data-diff-workers")).toBe(
      "shared",
    );
  });

  it("keeps a review open while the pool becomes ready, and hands the next one the pool", async () => {
    workers.behaviour = "answer";
    const first = render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    const container = document.querySelector("diffs-container");
    // The first review started the pool for the next.
    await waitFor(() => expect(shared.status()).toBe("ready"));
    expect(document.querySelector("diffs-container")).toBe(container);
    expect(drawn()).toContain("const two = 2;");
    first.unmount();
    const started = workers.made.length;
    render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    // No new workers for the second review: it took the pool already started.
    expect(workers.made.length).toBe(started);
  });

  it("keeps a given-up pool for its review through rerenders, until the review closes", async () => {
    workers.behaviour = "answer";
    shared.start();
    await waitFor(() => expect(shared.status()).toBe("ready"));
    const pool = shared.peekReady()!;
    const terminate = vi.spyOn(pool, "terminate");
    const open = render(review());
    await waitFor(() => expect(drawn()).toContain("const two = 2;"));
    act(() => {
      workers.made[0]!.dispatchEvent(new Event("error"));
    });
    expect(shared.status()).toBe("failed");
    open.rerender(review());
    expect(terminate).not.toHaveBeenCalled();
    expect(drawn()).toContain("const two = 2;");
    open.unmount();
    expect(terminate).toHaveBeenCalledTimes(1);
  });

  it("draws each review's own text when two name the same file and revision", async () => {
    // Two sessions each deleted a one-line src/app.ts: same path, same counts, different code.
    const revision = `${PATH}:deleted:::0:1:1:`;
    workers.behaviour = "answer";
    shared.start();
    await waitFor(() => expect(shared.status()).toBe("ready"));
    const first = render(
      review({ before: "const fromFirstSession = 1;\n", after: null, revision }),
    );
    await waitFor(() => expect(workers.rendered).toHaveLength(1));
    await waitFor(() => expect(drawn()).toContain("fromFirstSession"));
    first.unmount();
    render(review({ before: "const fromSecondSession = 2;\n", after: null, revision }));
    // The shared pool rendered the second file afresh rather than reusing the first's result.
    await waitFor(() => expect(workers.rendered).toHaveLength(2));
    await waitFor(() => expect(drawn()).toContain("fromSecondSession"));
    expect(drawn()).not.toContain("fromFirstSession");
    expect(document.querySelector("[data-diff-workers]")?.getAttribute("data-diff-workers")).toBe(
      "shared",
    );
  });
});
