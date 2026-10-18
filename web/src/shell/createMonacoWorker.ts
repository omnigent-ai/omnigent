/** Route refinement to an independently cancellable worker using bundler-visible URLs. */
export function createMonacoWorker() {
  const worker = new Worker(new URL("./monacoWorker.js", import.meta.url), { type: "module" });
  const refinements = new Map<number, Worker>();
  worker.addEventListener("message", (event) => {
    const { data } = event;
    if (data?.type !== "refine-diff" && data?.type !== "cancel-refinement") return;
    event.stopImmediatePropagation();
    const stop = () => {
      refinements.get(data.id)?.terminate();
      refinements.delete(data.id);
    };
    if (data.type === "cancel-refinement") {
      stop();
      return;
    }
    const finish = (result: unknown) => {
      stop();
      worker.postMessage({ type: "refined-diff", id: data.id, result });
    };
    try {
      const refinement = new Worker(new URL("./monacoRefinement.worker.js", import.meta.url), {
        type: "module",
      });
      refinements.set(data.id, refinement);
      refinement.onmessage = ({ data: result }) => finish(result);
      refinement.onerror = (error) => {
        error.preventDefault();
        finish(null);
      };
      refinement.onmessageerror = () => finish(null);
      refinement.postMessage(data.input);
    } catch {
      finish(null);
    }
  });
  const terminate = worker.terminate.bind(worker);
  worker.terminate = () => {
    for (const refinement of refinements.values()) refinement.terminate();
    refinements.clear();
    terminate();
  };
  return worker;
}
