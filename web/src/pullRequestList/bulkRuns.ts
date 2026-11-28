// A bulk PR action's run, kept outside React: it goes on after the view that started it closes,
// and one toast per run shows how far it got, offers Stop, and becomes its summary at the end.

import { toast } from "sonner";

export type BulkRunState = "running" | "finished" | "stopped";

export interface BulkRunSnapshot {
  readonly id: string;
  readonly label: string;
  readonly total: number;
  /** How many have ended, whatever their outcome. */
  readonly done: number;
  readonly failed: number;
  readonly state: BulkRunState;
  /** Whether the user asked it to stop, so it starts nothing more and offers no Stop. */
  readonly stopRequested: boolean;
}

/** What a run's toast says once it's over. */
export interface BulkRunSummary {
  title: string;
  detail?: string | null;
  /** Keep the toast open until it's closed, as for a failure the user should look at. */
  needsLook?: boolean;
}

/** The run's own handle, for whoever does its work. */
export interface BulkRunHandle {
  readonly id: string;
  /** The toast the run shows its progress in, for a summary its caller shows there itself. */
  readonly toastId: string;
  /** Whether the user asked to stop: start nothing more, and let what's in flight settle. */
  stopRequested: () => boolean;
  progress: (progress: { done: number; failed?: number }) => void;
  /** End the run with its summary; later calls do nothing. */
  finish: (summary: BulkRunSummary) => void;
  /** End the run whose caller shows its own summary in `toastId`. */
  end: () => void;
}

interface RunRecord {
  snapshot: BulkRunSnapshot;
  toastId: string;
  stop: boolean;
  onStop?: () => void;
}

const runs = new Map<string, RunRecord>();
const listeners = new Set<() => void>();
let runCount = 0;

function publish(record: RunRecord, next: Partial<BulkRunSnapshot>) {
  record.snapshot = { ...record.snapshot, ...next };
  for (const listener of listeners) listener();
}

function showProgress(record: RunRecord) {
  const { id, label, total, done, failed } = record.snapshot;
  // Not `toast.loading`: Sonner gives loading toasts no close button.
  toast(label, {
    id: record.toastId,
    description: `${done} of ${total} done${failed > 0 ? ` · ${failed} failed` : ""}`,
    duration: Number.POSITIVE_INFINITY,
    action: record.stop
      ? undefined
      : {
          label: "Stop",
          onClick: (event) => {
            // The toast stays to say what happened once the run settles.
            event.preventDefault();
            stopBulkRun(id);
          },
        },
  });
}

/** Start a run of `total` changes; call it from the click that confirmed them. */
export function startBulkRun({
  label,
  total,
  toastId,
  onStop,
}: {
  label: string;
  total: number;
  /** A toast to show it in, such as one an Undo of it is held in; its own otherwise. */
  toastId?: string;
  /** Cancel what's in flight, for a run whose one request can be stopped. */
  onStop?: () => void;
}): BulkRunHandle {
  const id = String(++runCount);
  const record: RunRecord = {
    snapshot: { id, label, total, done: 0, failed: 0, state: "running", stopRequested: false },
    toastId: toastId ?? `bulk-run:${id}`,
    stop: false,
    onStop,
  };
  runs.set(id, record);
  publish(record, {});
  showProgress(record);
  const end = () => {
    if (record.snapshot.state !== "running") return false;
    publish(record, { state: record.stop ? "stopped" : "finished" });
    runs.delete(id);
    // Readers of the run by its id hear that it's gone, not only that it ended.
    for (const listener of listeners) listener();
    return true;
  };
  return {
    id,
    toastId: record.toastId,
    stopRequested: () => record.stop,
    progress: ({ done, failed = record.snapshot.failed }) => {
      if (record.snapshot.state !== "running") return;
      publish(record, { done, failed });
      showProgress(record);
    },
    finish: (summary) => {
      if (!end()) return;
      toast(summary.title, {
        id: record.toastId,
        description: summary.detail ?? undefined,
        action: undefined,
        ...(summary.needsLook
          ? { duration: Number.POSITIVE_INFINITY, closeButton: true }
          : { duration: undefined }),
      });
    },
    end: () => {
      end();
    },
  };
}

/** Ask a running run to stop: nothing more starts, and its summary says so. */
export function stopBulkRun(id: string): void {
  const record = runs.get(id);
  if (!record || record.snapshot.state !== "running" || record.stop) return;
  record.stop = true;
  publish(record, { stopRequested: true });
  showProgress(record);
  record.onStop?.();
}

/** The runs still going, oldest first. */
export function runningBulkRuns(): BulkRunSnapshot[] {
  return [...runs.values()].map((record) => record.snapshot);
}

/** A running run as it is now; the same object until it changes, null once it has ended. */
export function bulkRunSnapshot(id: string): BulkRunSnapshot | null {
  return runs.get(id)?.snapshot ?? null;
}

export function subscribeBulkRuns(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forget every run, for tests. */
export function resetBulkRunsForTests(): void {
  runs.clear();
  runCount = 0;
}
