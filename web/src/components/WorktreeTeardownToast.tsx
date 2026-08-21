// Toast for a failed project worktree teardown script
// (`worktree_pre_delete_command`). The script runs just before Omnigent
// removes a session's worktree; the delete proceeds regardless (fail-open),
// so the only thing left to do is tell the user — and the session row is
// already gone, so there is nowhere to persist a banner instead.
//
// Lives in its own module because `useConversations.ts` (where deletes are
// mutated) is plain TS and can't build the collapsible output itself.

import { showToast } from "@/components/ui/toast";

/** One failed teardown script, for the aggregate (bulk-delete) toast. */
export interface TeardownFailure {
  /** Human-readable session label, e.g. `"Fix login bug"`. */
  label: string;
  /** One-line cause, e.g. `"exited with status 1"`. */
  reason: string;
  /** Last 10 KB of the script's combined output; may be empty. */
  outputTail: string;
}

/** Collapsible output block, rendered under the toast's headline. */
function OutputDetails({ failures }: { failures: TeardownFailure[] }) {
  const withOutput = failures.filter((f) => f.outputTail.trim() !== "");
  if (withOutput.length === 0) return null;
  return (
    <details className="mt-1" data-testid="teardown-toast-output">
      <summary className="cursor-pointer text-muted-foreground text-sm">View output</summary>
      {withOutput.map((f) => (
        <pre
          key={`${f.label}:${f.reason}`}
          className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-muted/50 p-2 font-mono text-xs"
        >
          {withOutput.length > 1 ? `${f.label}\n${f.outputTail}` : f.outputTail}
        </pre>
      ))}
    </details>
  );
}

/**
 * Toast a failed teardown script for a single deleted session.
 *
 * @param failure - The failed script's label, reason, and output.
 */
export function showTeardownFailureToast(failure: TeardownFailure): void {
  showToast(
    <div data-testid="teardown-toast">
      <p>
        Teardown script for {failure.label} {failure.reason} — the session was still deleted.
      </p>
      <OutputDetails failures={[failure]} />
    </div>,
  );
}

/**
 * Toast one aggregate line for several failed teardown scripts.
 *
 * A bulk delete runs one script per worktree; N separate toasts would bury
 * the sidebar, so they collapse into a single count with the outputs stacked
 * behind one disclosure.
 *
 * @param failures - The failed scripts.
 * @param total - How many sessions the bulk delete covered.
 */
export function showBulkTeardownFailureToast(failures: TeardownFailure[], total: number): void {
  if (failures.length === 0) return;
  if (failures.length === 1 && total === 1) {
    showTeardownFailureToast(failures[0]);
    return;
  }
  showToast(
    <div data-testid="teardown-toast">
      <p>
        {failures.length} of {total} teardown scripts failed — the sessions were still deleted.
      </p>
      <OutputDetails failures={failures} />
    </div>,
  );
}
