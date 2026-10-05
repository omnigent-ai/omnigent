// Importing a design system: the confirm summary, copy progress with per-file
// errors, and the studio dialog that runs the whole flow for a pointed design.

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { planDesignSystemImportFrom, runDesignSystemImport } from "@/lib/designDeckApi";
import { DESIGN_SYSTEM_IMPORT_DIR, type DesignSystemRef } from "@/lib/designSystem";
import type { ImportError, ImportPlan } from "@/lib/designSystemImport";
import { formatBytes } from "@/shell/fileStatusUtils";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const message = (e: unknown) => (e instanceof Error && e.message ? e.message : String(e));

export function ImportSummary({ plan }: { plan: ImportPlan }) {
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="design-system-import-summary">
      <p>
        {plan.files.length
          ? `Copies ${plural(plan.files.length, "file")} (${formatBytes(plan.totalBytes)}) into ${DESIGN_SYSTEM_IMPORT_DIR}.`
          : "Nothing to import: no design-system files were found."}
      </p>
      {plan.skipped.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            {`Skips ${plural(plan.skipped.length, "file")}`}
          </summary>
          <ul className="max-h-24 overflow-y-auto font-mono text-xs text-muted-foreground">
            {plan.skipped.map((s) => (
              <li key={s.path}>{`${s.path}: ${s.reason}`}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function ImportProgress({
  done,
  total,
  errors,
}: {
  done: number;
  total: number;
  errors: readonly ImportError[];
}) {
  return (
    <div className="flex flex-col gap-1 text-sm">
      <p role="status">{`Copied ${done} of ${plural(total, "file")}`}</p>
      <progress className="w-full" value={done} max={Math.max(total, 1)} />
      {errors.length > 0 && (
        <ul className="max-h-24 overflow-y-auto font-mono text-xs text-destructive">
          {errors.map((e) => (
            <li key={e.path}>{`${e.path}: ${e.message}`}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

type Step =
  | { step: "planning" }
  | { step: "confirm"; plan: ImportPlan }
  | { step: "copying" | "failed"; plan: ImportPlan; done: number; errors: ImportError[] }
  | { step: "error"; message: string };

/** Import a pointed design's outside folder into its workspace. */
export function ImportDesignSystemDialog({
  open,
  onOpenChange,
  sessionId,
  hostId,
  source,
  onImported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  hostId: string;
  source: DesignSystemRef;
  onImported: () => void;
}) {
  const [state, setState] = useState<Step>({ step: "planning" });
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setState({ step: "planning" });
    planDesignSystemImportFrom(hostId, source.path).then(
      (plan) => !cancelled && setState({ step: "confirm", plan }),
      (e: unknown) => !cancelled && setState({ step: "error", message: message(e) }),
    );
    return () => {
      cancelled = true;
    };
  }, [open, hostId, source.path]);

  async function copy(plan: ImportPlan) {
    setState({ step: "copying", plan, done: 0, errors: [] });
    try {
      const result = await runDesignSystemImport(sessionId, plan, source, (done) =>
        setState({ step: "copying", plan, done, errors: [] }),
      );
      if (result.errors.length) {
        setState({ step: "failed", plan, done: plan.files.length, errors: result.errors });
        return;
      }
      onImported();
      onOpenChange(false);
    } catch (e) {
      setState({ step: "error", message: message(e) });
    }
  }

  const busy = state.step === "copying";
  const plan = "plan" in state ? state.plan : null;
  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-[480px]" data-testid="import-design-system-dialog">
        <DialogHeader>
          <DialogTitle>Import design system</DialogTitle>
          <DialogDescription>
            {`Copies ${source.name} from ${source.path} into this design's workspace, so collaborators see the branding.`}
          </DialogDescription>
        </DialogHeader>
        {state.step === "planning" && <Spinner className="size-5" aria-label="Listing files" />}
        {state.step === "error" && (
          <p role="alert" className="text-sm text-destructive">
            {`Couldn't import: ${state.message}`}
          </p>
        )}
        {state.step === "confirm" && <ImportSummary plan={state.plan} />}
        {(state.step === "copying" || state.step === "failed") && (
          <ImportProgress done={state.done} total={state.plan.files.length} errors={state.errors} />
        )}
        <DialogFooter>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => onOpenChange(false)}
            componentId="design.import.cancel"
          >
            Cancel
          </Button>
          {plan && state.step !== "copying" && (
            <Button
              disabled={plan.files.length === 0}
              onClick={() => void copy(plan)}
              componentId="design.import.confirm"
            >
              {state.step === "failed" ? "Retry" : "Import"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
