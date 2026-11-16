import { Button } from "@/components/ui/button";
import type { CellRunResult } from "@/lib/cellRunsApi";
import { cn } from "@/lib/utils";
import { XIcon } from "lucide-react";
import { useCallback, useSyncExternalStore } from "react";

export type CodeCellRunState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "done"; result: CellRunResult }
  | { phase: "error"; message: string };

/** Languages whose chat code blocks offer Run when cell runs are available. */
export function isRunnableCellLanguage(language: string): boolean {
  return /^(python|python3|py)$/.test(language);
}

export interface CellRunAction {
  /** Session the runs belong to, e.g. "conv_abc123". */
  sessionId: string;
  run: (code: string) => Promise<CellRunResult>;
}

const IDLE: CodeCellRunState = { phase: "idle" };
// Shared by session and source, so a remounted block keeps its run and can't start a second one.
const cellRuns = new Map<string, CodeCellRunState>();
const cellRunListeners = new Set<() => void>();

function setCellRun(key: string, state: CodeCellRunState): void {
  if (state.phase === "idle") cellRuns.delete(key);
  else cellRuns.set(key, state);
  for (const listener of cellRunListeners) listener();
}

function subscribeCellRuns(listener: () => void): () => void {
  cellRunListeners.add(listener);
  return () => cellRunListeners.delete(listener);
}

/**
 * Track one code block's run. A run starts only from `start`, i.e. a click.
 *
 * @param action - Starts runs in the open session, or `null` when unavailable.
 * @param code - The block's source, sent as-is.
 */
export function useCodeCellRun(
  action: CellRunAction | null,
  code: string,
): { state: CodeCellRunState; start: () => void; dismiss: () => void } {
  const key = action ? `${action.sessionId}\u0000${code}` : null;
  const state = useSyncExternalStore(subscribeCellRuns, () =>
    key === null ? IDLE : (cellRuns.get(key) ?? IDLE),
  );
  const start = useCallback(() => {
    if (!action || key === null || !code.trim()) return;
    if (cellRuns.get(key)?.phase === "running") return;
    setCellRun(key, { phase: "running" });
    action.run(code).then(
      (result) => setCellRun(key, { phase: "done", result }),
      (error: unknown) => {
        const message = error instanceof Error && error.message ? error.message : "";
        setCellRun(key, { phase: "error", message: message || "The cell couldn't run." });
      },
    );
  }, [action, key, code]);
  const dismiss = useCallback(() => {
    if (key !== null && cellRuns.get(key)?.phase !== "running") setCellRun(key, IDLE);
  }, [key]);
  return { state, start, dismiss };
}

function seconds(durationMs: number): string {
  return `${(durationMs / 1000).toFixed(1)} s`;
}

function summary(state: Exclude<CodeCellRunState, { phase: "idle" }>): string {
  if (state.phase === "running") return "Running…";
  if (state.phase === "error") return "Couldn't run the cell";
  const { result } = state;
  if (result.status === "timed_out") return `Timed out after ${seconds(result.durationMs)}`;
  if (result.status === "failed") return "Couldn't run the cell";
  if (result.exitCode === 0) return `Finished in ${seconds(result.durationMs)}`;
  return `Exited with code ${result.exitCode} after ${seconds(result.durationMs)}`;
}

const STREAM_CLASS = "max-h-80 overflow-auto px-2.5 py-2 font-mono whitespace-pre-wrap break-words";

/** Output of a code block's run, shown under the block until dismissed. */
export function CodeCellOutput({
  state,
  onDismiss,
}: {
  state: CodeCellRunState;
  onDismiss: () => void;
}) {
  if (state.phase === "idle") return null;
  const result = state.phase === "done" ? state.result : null;
  const failure = state.phase === "error" ? state.message : (result?.error ?? null);
  const empty = result !== null && result.status !== "failed" && !result.stdout && !result.stderr;
  return (
    <section
      aria-label="Cell output"
      aria-busy={state.phase === "running"}
      className="mt-1.5 overflow-hidden rounded-md border border-border bg-muted/30 text-xs"
    >
      <div className="flex min-h-8 items-center justify-between gap-2 px-2.5 text-muted-foreground">
        <span role="status">{summary(state)}</span>
        {state.phase !== "running" && (
          <Button
            aria-label="Clear output"
            className="size-6"
            onClick={onDismiss}
            size="icon-sm"
            title="Clear output"
            type="button"
            variant="ghost"
          >
            <XIcon aria-hidden="true" />
          </Button>
        )}
      </div>
      {result?.stdout ? (
        <pre aria-label="Standard output" className={cn(STREAM_CLASS, "border-t border-border")}>
          {result.stdout}
        </pre>
      ) : null}
      {result?.stderr ? (
        <pre
          aria-label="Standard error"
          className={cn(STREAM_CLASS, "border-t border-border text-destructive")}
        >
          {result.stderr}
        </pre>
      ) : null}
      {result && (result.stdoutTruncated || result.stderrTruncated) ? (
        <p className="border-t border-border px-2.5 py-1.5 text-muted-foreground">
          Output was cut off.
        </p>
      ) : null}
      {empty ? (
        <p className="border-t border-border px-2.5 py-1.5 text-muted-foreground">No output</p>
      ) : null}
      {failure && state.phase !== "running" && result?.status !== "completed" ? (
        <p className="border-t border-border px-2.5 py-1.5 text-destructive">{failure}</p>
      ) : null}
    </section>
  );
}
