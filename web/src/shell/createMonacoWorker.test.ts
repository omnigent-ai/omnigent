import { afterEach, expect, it, vi } from "vitest";
import { createMonacoWorker } from "./createMonacoWorker";

function setup(failConstruction = false) {
  const workers: FakeWorker[] = [];
  class FakeWorker extends EventTarget {
    postMessage = vi.fn();
    terminate = vi.fn();
    onmessage: ((event: { data: unknown }) => void) | null = null;
    onerror: ((event: { preventDefault: () => void }) => void) | null = null;
    onmessageerror: (() => void) | null = null;
    constructor() {
      super();
      if (failConstruction && workers.length) throw new Error("Worker unavailable");
      workers.push(this);
    }
  }
  vi.stubGlobal("Worker", FakeWorker);
  const parent = createMonacoWorker();
  const request = () =>
    parent.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "refine-diff", id: 1, input: { before: ["a"], after: ["b"] } },
      }),
    );
  return { parent, workers, request };
}
afterEach(() => vi.unstubAllGlobals());

it("keeps control messages out of Monaco RPC and returns refined results", () => {
  const { parent, workers, request } = setup();
  const monacoListener = vi.fn();
  parent.addEventListener("message", monacoListener);
  request();
  expect(monacoListener).not.toHaveBeenCalled();
  expect(workers[1].postMessage).toHaveBeenCalledWith({ before: ["a"], after: ["b"] });
  workers[1].onmessage?.({ data: "result" });
  expect(workers[0].postMessage).toHaveBeenCalledWith({
    type: "refined-diff",
    id: 1,
    result: "result",
  });
  expect(workers[1].terminate).toHaveBeenCalledTimes(1);
});

it.each(["cancel", "parent-disposal", "error", "messageerror"])(
  "terminates refinement on %s",
  (reason) => {
    const { parent, workers, request } = setup();
    request();
    if (reason === "cancel")
      parent.dispatchEvent(
        new MessageEvent("message", { data: { type: "cancel-refinement", id: 1 } }),
      );
    if (reason === "parent-disposal") parent.terminate();
    if (reason === "error") workers[1].onerror?.({ preventDefault: vi.fn() });
    if (reason === "messageerror") workers[1].onmessageerror?.();
    expect(workers[1].terminate).toHaveBeenCalledTimes(1);
    if (reason === "error" || reason === "messageerror")
      expect(workers[0].postMessage).toHaveBeenCalledWith({
        type: "refined-diff",
        id: 1,
        result: null,
      });
  },
);

it("settles refinement when the child worker cannot be constructed", () => {
  const { parent, workers, request } = setup(true);
  request();
  expect(workers).toHaveLength(1);
  expect(workers[0].postMessage).toHaveBeenCalledExactlyOnceWith({
    type: "refined-diff",
    id: 1,
    result: null,
  });
  expect(() => parent.terminate()).not.toThrow();
});
