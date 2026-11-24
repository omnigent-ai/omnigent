import { ListTodoIcon } from "lucide-react";
import type { PlanProgressLine } from "@/lib/sessionPlanProgress";

const LEAD_TONES = {
  waiting: "font-medium text-brand-accent",
  failed: "font-medium text-destructive",
  muted: "",
} as const;

/**
 * A workflow run's line under its sidebar title, "3/5 · Open PRs for safe
 * upgrades": its finished steps, then the step it's on, or how it finished.
 */
export function SessionPlanLine({ line }: { line: PlanProgressLine }) {
  return (
    <span
      data-testid="session-row-plan"
      className="flex w-full min-w-0 items-center gap-1 text-sm text-muted-foreground"
    >
      <ListTodoIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="sr-only">{`Tasks: ${line.finished} of ${line.total} finished, `}</span>
      <span aria-hidden className="shrink-0 tabular-nums">
        {line.finished}/{line.total}
      </span>
      <span aria-hidden className="shrink-0">
        ·
      </span>
      <span className="min-w-0 truncate">
        {line.lead && (
          <>
            <span className={LEAD_TONES[line.lead.tone]}>{line.lead.text}</span>:{" "}
          </>
        )}
        {line.text}
      </span>
      {line.more > 0 && (
        <span className="shrink-0">
          <span aria-hidden>+{line.more}</span>
          <span className="sr-only">{` and ${line.more} more`}</span>
        </span>
      )}
    </span>
  );
}
