import type { editor } from "monaco-editor";

export const INITIAL_DIFF_BUDGET_MS = 1000;
const REFINEMENT_BUDGET_MS = 10000;
export type DiffRefinementStatus = "idle" | "refining" | "limited";

// Monaco exposes timeout information at runtime, but omits it from its public types.
type DiffResultEditor = editor.IStandaloneDiffEditor & {
  getDiffComputationResult?: () => { quitEarly: boolean } | null;
};

/** Retry once per content/settings state; the owning viewer keeps models stable per mount. */
export function attachAutomaticDiffRefinement(
  diffEditor: editor.IStandaloneDiffEditor,
  onStatus: (status: DiffRefinementStatus) => void,
  inspectExistingResult = true,
  onBudget: (budget: number) => void = () => {},
) {
  const editor = diffEditor as DiffResultEditor;
  if (typeof editor.getDiffComputationResult !== "function") return () => {};
  let disposed = false;
  let attempted = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clearTimer = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  const setBudget = (budget: number) => {
    editor.updateOptions({ maxComputationTime: budget });
    onBudget(budget);
  };
  const reset = () => {
    clearTimer();
    attempted = false;
    onStatus("idle");
    setBudget(INITIAL_DIFF_BUDGET_MS);
  };
  const updated = () => {
    if (disposed) return;
    const result = editor.getDiffComputationResult?.();
    if (!result) return;
    if (!result.quitEarly) {
      clearTimer();
      onStatus("idle");
    } else if (!attempted) {
      attempted = true;
      onStatus("refining");
      // Give the initial highlighting a chance to paint before queuing more work.
      timer = setTimeout(() => {
        timer = undefined;
        if (!disposed) setBudget(REFINEMENT_BUDGET_MS);
      }, 150);
    } else if (timer === undefined) {
      onStatus("limited");
    }
  };
  const subscription = editor.onDidUpdateDiff(updated);
  const model = editor.getModel();
  const original = model?.original.onDidChangeContent(reset);
  const modified = model?.modified.onDidChangeContent(reset);
  if (inspectExistingResult) updated();
  return () => {
    disposed = true;
    clearTimer();
    subscription.dispose();
    original?.dispose();
    modified?.dispose();
  };
}
