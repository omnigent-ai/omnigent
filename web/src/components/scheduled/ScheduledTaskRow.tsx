// One row in the Tasks list, rendered as a FLAT list row (no card chrome — no
// border/background/shadow box, no per-row divider). Layout: bold task title on
// line 1 (+ a small "Paused" pill when paused), a single muted subline on line 2
// with the human-readable schedule summary ("Weekdays at 8:00 AM") followed by
// the next-run delta ("· Next run in 15h") when armed.
//
// The next-run delta is derived from the scheduler's authoritative `nextRunAt`
// (an ISO string the server computes): we format only how far away it is
// (nextRunAt − now), never recompute WHICH instant is next on the client — so
// the old "no client countdown" rule (a client-recomputed instant can't match
// the server anchor for INTERVAL>1 rules) is not violated. Paused rows are NOT
// dimmed — the title stays fully legible and the pill is the sole paused
// signal. A hover-revealed ellipsis (⋯) action menu (Run now / Pause /
// Resume / Edit / Delete) sits on the right.

import { useMemo, useState } from "react";
import {
  MoreHorizontalIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  Trash2Icon,
  ZapIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { describeSchedule, formatNextRunAt } from "@/lib/scheduleText";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

export function ScheduledTaskRow({
  task,
  onEdit,
  onPauseToggle,
  onRunNow,
  onDelete,
  busy,
}: {
  task: ScheduledTask;
  onEdit: (task: ScheduledTask) => void;
  onPauseToggle: (task: ScheduledTask) => void;
  onRunNow: (task: ScheduledTask) => void;
  onDelete: (task: ScheduledTask) => void;
  busy: boolean;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const paused = task.state === "paused";
  // Subtitle: the schedule summary, plus the SERVER's next-run time when armed
  // (active tasks only — a paused task has null nextRunAt). We only format the
  // server value; we never recompute next-run on the client.
  const scheduleSummary = useMemo(() => describeSchedule(task.rrule), [task.rrule]);
  const nextRun = useMemo(() => formatNextRunAt(task.nextRunAt), [task.nextRunAt]);

  return (
    <div
      data-testid="scheduled-task-row"
      data-state={task.state}
      className={cn(
        // Flat row — NO card chrome (no border/bg/shadow box) and no per-row
        // divider. `group relative` so the absolutely-positioned ⋯ trigger can
        // hover-reveal; vertical padding gives the flat-list spacing.
        // `-mx-2 rounded-lg` + `hover:bg-muted/70` gives a subtle FULL-ROW hover
        // highlight (like the sidebar conversation rows) that extends past the
        // content while keeping the title aligned with the page. `pl-2` (left
        // content inset) is mirrored by the ⋯ button's `right-2` inset so the
        // two edges are symmetric within the highlight; `pr-10` keeps the text
        // clear of the inset button. Paused rows are NOT dimmed — the title must
        // stay legible (AA); the "Paused" pill is the sole signal.
        "group relative -mx-2 flex items-center gap-3 rounded-lg py-[11px] pr-10 pl-2 transition-colors hover:bg-muted/70",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[15px] font-semibold">{task.name}</span>
          {paused && (
            <span
              data-testid="task-paused-pill"
              className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
            >
              Paused
            </span>
          )}
        </span>
        <span
          className="truncate text-[13px] text-muted-foreground/80"
          data-testid="task-schedule-line"
        >
          {scheduleSummary}
          {nextRun && (
            <>
              {" · "}
              <span data-testid="task-next-run">Next run {nextRun}</span>
            </>
          )}
        </span>
      </div>

      {/* Hover-revealed ellipsis menu, mirroring the sidebar conversation-row
          action button: absolute-positioned on the right, hidden until the row
          is hovered / focused, and kept surfaced while the menu is open via
          `aria-expanded`. */}
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Actions for ${task.name}`}
            data-testid="task-row-menu"
            disabled={busy}
            className={cn(
              "-translate-y-1/2 absolute top-1/2 right-2 transition-opacity",
              "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
              "md:aria-expanded:opacity-100",
            )}
          >
            <MoreHorizontalIcon className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => onRunNow(task)} data-testid="task-run-now">
            <ZapIcon className="size-4" />
            Run now
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onEdit(task)} data-testid="task-edit">
            <PencilIcon className="size-4" />
            Edit
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onPauseToggle(task)} data-testid="task-pause-toggle">
            {paused ? (
              <>
                <PlayIcon className="size-4" />
                Resume
              </>
            ) : (
              <>
                <PauseIcon className="size-4" />
                Pause
              </>
            )}
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => onDelete(task)}
            data-testid="task-delete"
          >
            <Trash2Icon className="size-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
