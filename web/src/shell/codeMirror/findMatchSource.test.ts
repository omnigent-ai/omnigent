import { SearchQuery, search, setSearchQuery } from "@codemirror/search";
import { EditorState, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FindQuerySpec, MatchBatch } from "./findMatchCollector";
import {
  createFindWorkerHandler,
  type FindWorkerBatch,
  type FindWorkerRequest,
} from "./findMatchWorkerProtocol";
import { FindMatchIndex } from "./codeMirrorFind";
import { type FindWorkerPort, inlineMatchSource, workerMatchSource } from "./findMatchSource";

afterEach(() => vi.restoreAllMocks());

const LITERAL: FindQuerySpec = {
  search: "hit",
  caseSensitive: false,
  wholeWord: false,
  regexp: false,
};

/** A worker whose handler runs when the test delivers its queued messages. */
function fakeWorker() {
  const inbox: FindWorkerRequest[] = [];
  const resumes: (() => void)[] = [];
  let port!: FindWorkerPort;
  const handle = createFindWorkerHandler(
    (batch) => port.onmessage?.({ data: batch } as MessageEvent<FindWorkerBatch>),
    (resume) => resumes.push(resume),
  );
  port = {
    postMessage: (message) => inbox.push(message),
    onmessage: null,
    onerror: null,
    terminate: vi.fn(),
  };
  return {
    port,
    inbox,
    /** Delivers queued messages, then runs the worker until it is idle. */
    run: () => {
      while (inbox.length || resumes.length) {
        if (inbox.length) handle(inbox.shift()!);
        else resumes.shift()!();
      }
    },
  };
}

function collect(source: ReturnType<typeof workerMatchSource>, doc: Text, spec = LITERAL) {
  const batches: MatchBatch[] = [];
  const cancel = source.start(doc, spec, (batch) => batches.push(batch));
  const froms = () => batches.flatMap((batch) => batch.froms);
  return { batches, cancel, froms, done: () => batches.at(-1)?.done === true };
}

describe("workerMatchSource", () => {
  it("collects every match in the worker", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port);
    const job = collect(source, Text.of(["hit and hit", "hit"]));
    worker.run();
    expect(job.froms()).toEqual([0, 8, 12]);
    expect(job.done()).toBe(true);
  });

  it("reports matches that cover only part of a character", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port);
    const job = collect(source, Text.of(["café e"]), { ...LITERAL, search: "e" });
    worker.run();
    expect(job.froms()).toEqual([3, 5]);
    expect(job.batches.flatMap((batch) => batch.imprecise)).toEqual([3]);
  });

  it("sends a document once and reuses it for later queries", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port);
    const doc = Text.of(["hit"]);
    collect(source, doc).cancel();
    collect(source, doc, { ...LITERAL, search: "h" });
    expect(worker.inbox.filter((message) => message.type === "document")).toHaveLength(1);
  });

  it("ignores batches from a cancelled search", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port);
    const doc = Text.of(["hit"]);
    const stale = collect(source, doc);
    stale.cancel();
    const current = collect(source, doc, { ...LITERAL, search: "i" });
    worker.run();
    expect(stale.batches).toHaveLength(0);
    expect(current.froms()).toEqual([1]);
  });

  it("keeps a long line with no match off the main thread", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port);
    const getCursor = vi.spyOn(SearchQuery.prototype, "getCursor");
    const job = collect(source, Text.of(["x".repeat(2 * 1024 * 1024)]), {
      ...LITERAL,
      search: "not-present",
    });
    // Starting a search only posts messages; the scan happens when the worker runs.
    expect(getCursor).not.toHaveBeenCalled();
    worker.run();
    expect(job.done()).toBe(true);
    expect(job.froms()).toEqual([]);
  });

  it("falls back to main-thread matching when the worker fails", () => {
    const worker = fakeWorker();
    const source = workerMatchSource(() => worker.port, inlineMatchSource());
    const job = collect(source, Text.of(["hit hit"]));
    worker.port.onerror?.({} as ErrorEvent);
    expect(worker.port.terminate).toHaveBeenCalled();
    expect(job.froms()).toEqual([0, 4]);
    expect(job.done()).toBe(true);
  });

  it("replaces partial worker results when it fails over mid-search", () => {
    const port: FindWorkerPort = {
      postMessage: () => {},
      onmessage: null,
      onerror: null,
      terminate: vi.fn(),
    };
    const source = workerMatchSource(() => port, inlineMatchSource());
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({ doc: "hit hit hit", extensions: [search()] }),
    });
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: "hit" })) });
    const index = new FindMatchIndex(() => {}, source);
    index.sync(view.state);
    const jobId = 1;
    port.onmessage?.({
      data: {
        jobId,
        froms: Float64Array.of(0, 4),
        tos: Float64Array.of(3, 7),
        imprecise: new Float64Array(),
        done: false,
      },
    } as MessageEvent<FindWorkerBatch>);
    port.onerror?.({} as ErrorEvent);

    expect(index.status(view.state)).toMatchObject({ complete: true, matchCount: 3 });
    view.dispatch({ selection: { anchor: 8, head: 11 } });
    expect(index.status(view.state).ordinal).toBe(3);
    expect(index.lookup(1, view.state.selection.main)).toMatchObject({ from: 0, to: 3 });
    expect(index.lookup(-1, view.state.selection.main)).toMatchObject({ from: 4, to: 7 });
    view.destroy();
  });

  it("falls back when the worker can't be created", () => {
    const source = workerMatchSource(() => {
      throw new Error("blocked by policy");
    });
    const job = collect(source, Text.of(["hit"]));
    expect(job.froms()).toEqual([0]);
  });
});

describe("createFindWorkerHandler", () => {
  it("answers a search for a document it doesn't hold with no matches", () => {
    const posted: FindWorkerBatch[] = [];
    const handle = createFindWorkerHandler((batch) => posted.push(batch));
    handle({ type: "search", jobId: 1, documentId: 7, spec: LITERAL });
    expect(posted).toEqual([expect.objectContaining({ jobId: 1, done: true })]);
  });

  it("stops a search once it is cancelled", () => {
    const posted: FindWorkerBatch[] = [];
    const resumes: (() => void)[] = [];
    let time = 0;
    const handle = createFindWorkerHandler(
      (batch) => posted.push(batch),
      (resume) => resumes.push(resume),
      () => (time += 100),
    );
    handle({ type: "document", documentId: 1, text: "hit\n".repeat(10) });
    handle({ type: "search", jobId: 1, documentId: 1, spec: LITERAL });
    handle({ type: "cancel", jobId: 1 });
    while (resumes.length) resumes.shift()!();
    expect(posted).toHaveLength(1);
    expect(posted[0]!.done).toBe(false);
  });
});
