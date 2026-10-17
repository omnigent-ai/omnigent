import { INITIAL_DIFF_BUDGET_MS } from "./automaticDiffRefinement";
import { initialize } from "monaco-editor/esm/vs/base/common/worker/webWorkerBootstrap.js";
import { EditorWorker } from "monaco-editor/esm/vs/editor/common/services/editorWebWorker.js";

// Keep expensive refinement off the worker that serves newly opened files.
class RefiningEditorWorker extends EditorWorker {
  refinements = new Set();
  initialResults = new Map();
  nextRefinementId = 0;

  forgetInitialResults(uri) {
    for (const [modified, entry] of this.initialResults) {
      if (entry.original === uri || modified === uri) this.initialResults.delete(modified);
    }
  }

  cancelRefinements(uri) {
    for (const task of this.refinements) {
      if (task.original === uri || task.modified === uri) task.finish(null);
    }
  }

  $acceptRemovedModel(uri) {
    this.forgetInitialResults(uri);
    this.cancelRefinements(uri);
    super.$acceptRemovedModel(uri);
  }

  $acceptModelChanged(uri, event) {
    this.forgetInitialResults(uri);
    this.cancelRefinements(uri);
    super.$acceptModelChanged(uri, event);
  }

  async $computeDiff(original, modified, options, algorithm) {
    this.cancelRefinements(original);
    this.cancelRefinements(modified);
    const key = JSON.stringify([
      original,
      algorithm,
      { ...options, maxComputationTimeMs: undefined },
    ]);
    if (options.maxComputationTimeMs <= INITIAL_DIFF_BUDGET_MS) {
      const result = await super.$computeDiff(original, modified, options, algorithm);
      this.initialResults.delete(modified);
      if (result?.quitEarly) this.initialResults.set(modified, { original, key, result });
      return result;
    }
    const before = this.getModels().find((model) => model.uri.toString() === original);
    const after = this.getModels().find((model) => model.uri.toString() === modified);
    if (!before || !after) return null;
    // Keep the displayed detail on failure; invalidate saved results on content/settings changes.
    const initial = this.initialResults.get(modified);
    this.initialResults.delete(modified);
    const fallback =
      initial?.key === key
        ? initial.result
        : await super.$computeDiff(
            original,
            modified,
            { ...options, maxComputationTimeMs: 1 },
            algorithm,
          );
    return new Promise((resolve) => {
      const id = ++this.nextRefinementId;
      let finished = false;
      const task = {
        id,
        original,
        modified,
        finish: (result) => {
          if (finished) return;
          finished = true;
          clearTimeout(watchdog);
          self.postMessage({ type: "cancel-refinement", id });
          this.refinements.delete(task);
          resolve(result ?? fallback);
        },
      };
      // Also bound worker lifetime if loading or computation stalls.
      const watchdog = setTimeout(() => task.finish(null), options.maxComputationTimeMs + 2000);
      this.refinements.add(task);
      self.postMessage({
        type: "refine-diff",
        id,
        input: {
          before: before.getLinesContent(),
          after: after.getLinesContent(),
          options,
          algorithm,
        },
      });
    });
  }
}

self.onmessage = () => {
  const editor = new RefiningEditorWorker();
  initialize(() => editor);
  const handleMonacoMessage = self.onmessage;
  self.onmessage = (event) => {
    if (event.data?.type === "refined-diff") {
      for (const task of editor.refinements) {
        if (task.id === event.data.id) task.finish(event.data.result);
      }
    } else {
      handleMonacoMessage(event);
    }
  };
};
